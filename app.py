import streamlit as st
import fitz  # PyMuPDF
import tempfile
import os
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# -------------------------------------------------------------
# CLIENT SETUP
# -------------------------------------------------------------
API_KEY = str(st.secrets.get("GEMINI_API_KEY", "")).strip()

if not API_KEY:
    API_KEY = str(st.sidebar.text_input("Enter Gemini API Key", type="password")).strip()
    if not API_KEY:
        st.warning("Please configure your GEMINI_API_KEY to begin.")
        st.stop()

client = genai.Client(api_key=API_KEY)
MODEL_NAME = "gemini-1.5-flash"

# -------------------------------------------------------------
# STRUCTURED OUTPUT SCHEMA (PYDANTIC)
# -------------------------------------------------------------
class Annotation(BaseModel):
    y_position_percent: float = Field(description="Vertical percentage (0-100) where the student wrote their answer")
    question_ref: str = Field(description="e.g., '1(d)(i)' or '4(b)'")
    awarded_marks: int = Field(description="Marks awarded, e.g., 1 or 0")
    is_correct: bool
    comment: str = Field(description="Cambridge marking scheme reason/explanation")

class PageResult(BaseModel):
    page_index: int = Field(description="0-based physical page index")
    page_score: int
    annotations: list[Annotation]

class GradingResult(BaseModel):
    pages: list[PageResult]

GRADING_PROMPT = """
You are a senior Cambridge IGCSE Chemistry (0620 Paper 6) principal examiner.
Evaluate the complete student answer script against the official Cambridge Mark Scheme provided.

INSTRUCTIONS:
1. STRICT MARK SCHEME ADHERENCE: Award marks ONLY when the student satisfies the specific marking points (M1, A1, B1, etc.).
2. CROSSED-OUT WORK: If replaced, mark the replacement. If crossed out but not replaced, mark it as valid.
3. BLANKS & DOUBT: If blank, award [0] with comment 'Unattempted'. If 'doubt' is written, award [0] and explain the mark scheme point. Ignore pre-existing red markings.
4. ECF: Apply Error Carried Forward where appropriate.
5. VERTICAL ALIGNMENT: 'y_position_percent' MUST be the exact vertical coordinate (0 to 100) beside the student's handwritten answer line for that sub-question.
"""

def sanitize_text(text: str) -> str:
    replacements = {"✓": "[Y]", "✔": "[Y]", "✗": "[X]", "✘": "[X]", "±": "+/-", "°": " deg ", "→": "->", "Δ": "delta "}
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")

# -------------------------------------------------------------
# UI & STATE
# -------------------------------------------------------------
st.set_page_config(page_title="IGCSE Chemistry Grader", layout="centered")
st.title("🧪 IGCSE Chemistry Grader (GenAI SDK)")

if "ms_file" not in st.session_state:
    st.session_state.ms_file = None
if "ms_name" not in st.session_state:
    st.session_state.ms_name = None

# Step 1: Upload Mark Scheme to File API once
st.subheader("1. Mark Scheme")
ms_upload = st.file_uploader("Upload Cambridge Mark Scheme (PDF)", type=["pdf"], key="ms_uploader")

if ms_upload and st.session_state.ms_name != ms_upload.name:
    with st.spinner("Uploading Mark Scheme to Google API..."):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(ms_upload.read())
            tmp_path = tmp.name

        # Upload file natively via SDK
        uploaded_ms = client.files.upload(file=tmp_path)
        os.remove(tmp_path)

        st.session_state.ms_file = uploaded_ms
        st.session_state.ms_name = ms_upload.name
        st.success(f"Mark Scheme ready: **{st.session_state.ms_name}**")

elif st.session_state.ms_file:
    st.info(f"Active Mark Scheme: **{st.session_state.ms_name}**")
    if st.button("Change Mark Scheme"):
        st.session_state.ms_file = None
        st.session_state.ms_name = None
        st.rerun()
else:
    st.warning("Please upload a Mark Scheme first.")
    st.stop()

# Step 2: Student Submission
st.divider()
st.subheader("2. Student Submission")
student_upload = st.file_uploader("Upload Student PDF", type=["pdf"], key="student_uploader")

if student_upload and st.button("Grade Paper", type="primary"):
    with st.spinner("Uploading student paper and grading..."):
        student_bytes = student_upload.read()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(student_bytes)
            tmp_student_path = tmp.name

        # Upload student file via SDK
        uploaded_student_file = client.files.upload(file=tmp_student_path)
        os.remove(tmp_student_path)

        # Call generate_content with structured Pydantic schema
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=[
                    st.session_state.ms_file,
                    uploaded_student_file,
                    GRADING_PROMPT
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GradingResult,
                    temperature=0.1
                )
            )
            # SDK automatically parses it into the Pydantic model
            grading_result: GradingResult = response.parsed
        except Exception as e:
            st.error(f"Grading failed: {e}")
            st.stop()

        # Map pages into dictionary
        results = {p.page_index: p for p in grading_result.pages}

        # Annotate with PyMuPDF
        doc = fitz.open(stream=student_bytes, filetype="pdf")
        total_pages = len(doc)
        total_exam_score = 0
        is_1_indexed = (0 not in results) and (1 in results)

        for i in range(total_pages):
            page = doc[i]
            w, h = page.rect.width, page.rect.height
            scale = max(1.0, w / 595.0)

            lookup_key = (i + 1) if is_1_indexed else i
            page_obj = results.get(lookup_key)

            if not page_obj:
                continue

            total_exam_score += page_obj.page_score

            annotations = sorted(page_obj.annotations, key=lambda a: a.y_position_percent)

            margin_right = 10.0 * scale
            card_width = max(210.0 * scale, w * 0.33)
            x1 = w - margin_right
            x0 = max(10.0 * scale, x1 - card_width)
            base_font = 8.0 * scale
            usable_text_width = (x1 - x0) - (10.0 * scale)

            last_y_bottom = 12.0 * scale

            for item in annotations:
                prefix = f"[+{item.awarded_marks}]" if item.is_correct else f"[{item.awarded_marks}]"
                tag = f"{item.question_ref} {prefix}".strip()
                full_text = sanitize_text(f"{tag}: {item.comment}" if tag else item.comment)

                line_len = fitz.get_text_length(full_text, fontname="helv", fontsize=base_font)
                est_lines = max(1, int(line_len // usable_text_width) + full_text.count('\n') + 1)
                card_height = (est_lines * (base_font * 1.25)) + (8.0 * scale)

                target_y = (item.y_position_percent / 100.0) * h

                if target_y < last_y_bottom:
                    actual_y0 = last_y_bottom + (3.0 * scale)
                else:
                    actual_y0 = target_y

                actual_y1 = actual_y0 + card_height

                if actual_y1 > h - (10.0 * scale):
                    actual_y1 = h - (10.0 * scale)
                    actual_y0 = max(last_y_bottom, actual_y1 - card_height)

                rect = fitz.Rect(x0, actual_y0, x1, actual_y1)
                stroke_color = (0.15, 0.55, 0.15) if item.is_correct else (0.85, 0.15, 0.15)
                fill_color = (0.96, 1.0, 0.96) if item.is_correct else (1.0, 0.96, 0.96)
                text_color = (0.05, 0.40, 0.05) if item.is_correct else (0.75, 0.05, 0.05)

                page.draw_rect(rect, color=stroke_color, fill=fill_color, width=0.8 * scale)

                pad_x = 4.0 * scale
                pad_y = 3.0 * scale
                text_rect = fitz.Rect(rect.x0 + pad_x, rect.y0 + pad_y, rect.x1 - pad_x, rect.y1 - pad_y)

                rc = page.insert_textbox(text_rect, full_text, fontsize=base_font, fontname="helv", color=text_color, align=fitz.TEXT_ALIGN_LEFT)
                if rc < 0:
                    page.insert_textbox(text_rect, full_text, fontsize=base_font * 0.85, fontname="helv", color=text_color, align=fitz.TEXT_ALIGN_LEFT)

                last_y_bottom = actual_y1

        out_pdf_bytes = doc.write()
        doc.close()

    st.success(f"Grading Complete! Overall Exam Score: **{total_exam_score} / 40**")
    st.download_button(
        label="📥 Download Annotated PDF",
        data=out_pdf_bytes,
        file_name=f"graded_{student_upload.name}",
        mime="application/pdf"
    )