import streamlit as st
import fitz  # PyMuPDF
import requests
import json
import re
import base64
import time

# -------------------------------------------------------------
# CONFIGURATION & SECRETS
# -------------------------------------------------------------
API_KEY = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
MODEL = "gemini-3.5-flash"

if not API_KEY:
    API_KEY = str(st.sidebar.text_input("Enter Gemini API Key", type="password")).strip()
    if not API_KEY:
        st.warning("Please configure your GEMINI_API_KEY in Streamlit Secrets or sidebar.")
        st.stop()

GRADING_PROMPT = """
You are a senior Cambridge IGCSE Chemistry (0620 Paper 6) principal examiner.
Evaluate the complete student answer script against the official Cambridge Mark Scheme provided.

INSTRUCTIONS:
1. STRICT MARK SCHEME ADHERENCE:
   - Award marks ONLY when the student satisfies the specific marking points (M1, A1, B1, etc.).
2. CROSSED-OUT WORK:
   - If work is crossed out and replaced: evaluate ONLY the replacement answer.
   - If work is crossed out but NOT replaced: grade the crossed-out work as valid.
3. BLANK & DOUBT RESPONSES:
   - If an answer area is completely blank, award [0] and add a concise note: 'Unattempted' without shifting subsequent question alignment.
   - Ignore pre-existing red colored pen writing/ticks from previous checks and evaluate only the original blue or black student handwriting.
   - If you find the word doubt written in a question, award [0] and add a concise note explaining the question and the answer in the marking scheme.
4. ECF:
   - Account for error carried forward (ECF) where applicable.
5. VERTICAL ALIGNMENT RULE:
   - 'y_position_percent' MUST be the exact vertical coordinate (0 to 100) indicating where the student wrote their answer for that specific question line.
   - 0 is the top edge of the page, 100 is the bottom edge.

PAGE NUMBERING:
Use 0-based page indexing (First physical page = page_index 0).

OUTPUT FORMAT:
Output strictly valid JSON (no markdown fences, no explanation):
{
  "pages": [
    {
      "page_index": 0,
      "page_score": 6,
      "annotations": [
        {
          "y_position_percent": 35.0,
          "question_ref": "1(d)(i)",
          "awarded_marks": 1,
          "is_correct": true,
          "comment": "Correct observation: turns milky."
        }
      ]
    }
  ]
}
"""

def clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    start = text.find("{")
    if start == -1:
        return text
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text[start:])
        return json.dumps(obj)
    except Exception:
        match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        return match.group(1).strip() if match else text

def sanitize_text(text: str) -> str:
    replacements = {
        "✓": "[Y]", "✔": "[Y]", "✗": "[X]", "✘": "[X]",
        "±": "+/-", "°": " deg ", "→": "->", "Δ": "delta "
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")

# -------------------------------------------------------------
# UI SETUP & SESSION STATE
# -------------------------------------------------------------
st.set_page_config(page_title="IGCSE Paper 6 Grader", layout="centered")
st.title("🧪 IGCSE Chemistry Fast Grader")

if "ms_base64" not in st.session_state:
    st.session_state.ms_base64 = None
if "ms_file_name" not in st.session_state:
    st.session_state.ms_file_name = None

# Step 1: Mark Scheme
st.subheader("1. Mark Scheme")
ms_upload = st.file_uploader("Upload Cambridge Mark Scheme (PDF)", type=["pdf"], key="ms_uploader")

if ms_upload:
    st.session_state.ms_base64 = base64.b64encode(ms_upload.read()).decode("utf-8")
    st.session_state.ms_file_name = ms_upload.name
    st.success(f"Loaded Mark Scheme: **{st.session_state.ms_file_name}**")
elif st.session_state.ms_base64:
    st.info(f"Active Mark Scheme: **{st.session_state.ms_file_name}**")
    if st.button("Change Mark Scheme"):
        st.session_state.ms_base64 = None
        st.session_state.ms_file_name = None
        st.rerun()
else:
    st.warning("Please upload a Mark Scheme to proceed.")
    st.stop()

# Step 2: Single Student Paper
st.divider()
st.subheader("2. Student Submission")
uploaded_student = st.file_uploader("Upload Student PDF", type=["pdf"], key="single_student")

if uploaded_student and st.button("Grade Paper", type="primary"):
    with st.spinner("Grading script against mark scheme..."):
        student_bytes = uploaded_student.read()
        student_b64 = base64.b64encode(student_bytes).decode("utf-8")
        doc = fitz.open(stream=student_bytes, filetype="pdf")
        total_pages = len(doc)

        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "application/pdf", "data": st.session_state.ms_base64}},
                    {"inline_data": {"mime_type": "application/pdf", "data": student_b64}},
                    {"text": GRADING_PROMPT}
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 8192
            }
        }

        clean_model = str(MODEL).strip()
        clean_key = str(API_KEY).strip()
        target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={clean_key}"

        results = {}
        success = False

        for attempt in range(1, 4):
            gen_res = requests.post(target_url, json=payload, timeout=(10, 90))
            if gen_res.status_code == 200:
                raw_text = gen_res.json()["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    parsed_data = json.loads(clean_json(raw_text))
                    pages_list = parsed_data.get("pages", [])
                    results = {p.get("page_index", idx): p for idx, p in enumerate(pages_list)}
                    success = True
                    break
                except Exception:
                    if attempt < 3:
                        time.sleep(attempt * 2)
                        continue
            elif gen_res.status_code in (429, 503):
                time.sleep(attempt * 4)
            else:
                st.error(f"Inference error ({gen_res.status_code}): {gen_res.text}")
                break

        if not success:
            st.error("Failed to grade submission. Please try again.")
            st.stop()

        total_exam_score = 0
        is_1_indexed = (0 not in results) and (1 in results)

        for i in range(total_pages):
            page = doc[i]
            w = page.rect.width
            h = page.rect.height
            scale = max(1.0, w / 595.0)

            lookup_key = (i + 1) if is_1_indexed else i
            page_data = results.get(lookup_key, {"page_score": 0, "annotations": []})

            score = page_data.get("page_score", 0)
            total_exam_score += int(score) if str(score).isdigit() else 0

            annotations = page_data.get("annotations", [])

            def get_y_percent(ann):
                if "y_position_percent" in ann:
                    try:
                        return float(ann["y_position_percent"])
                    except (ValueError, TypeError):
                        pass
                return 30.0

            annotations.sort(key=get_y_percent)

            # Card positioning aligned right
            margin_right = 10.0 * scale
            card_width = max(210.0 * scale, w * 0.33)
            x1 = w - margin_right
            x0 = max(10.0 * scale, x1 - card_width)
            base_font = 8.0 * scale
            usable_text_width = (x1 - x0) - (10.0 * scale)

            last_y_bottom = 12.0 * scale

            for item in annotations:
                is_correct = item.get("is_correct", False) or "[1]" in item.get("comment", "") or "[+1]" in item.get("comment", "")
                q_ref = item.get("question_ref", "")
                marks = item.get("awarded_marks", 1 if is_correct else 0)
                comment = item.get("comment", "").strip()

                prefix = f"[+{marks}]" if is_correct else f"[{marks}]"
                tag = f"{q_ref} {prefix}".strip()
                full_text = sanitize_text(f"{tag}: {comment}" if tag else comment)

                line_len = fitz.get_text_length(full_text, fontname="helv", fontsize=base_font)
                est_lines = max(1, int(line_len // usable_text_width) + full_text.count('\n') + 1)
                card_height = (est_lines * (base_font * 1.25)) + (8.0 * scale)

                target_y = (get_y_percent(item) / 100.0) * h

                if target_y < last_y_bottom:
                    actual_y0 = last_y_bottom + (3.0 * scale)
                else:
                    actual_y0 = target_y

                actual_y1 = actual_y0 + card_height

                if actual_y1 > h - (10.0 * scale):
                    actual_y1 = h - (10.0 * scale)
                    actual_y0 = max(last_y_bottom, actual_y1 - card_height)

                rect = fitz.Rect(x0, actual_y0, x1, actual_y1)
                stroke_color = (0.15, 0.55, 0.15) if is_correct else (0.85, 0.15, 0.15)
                fill_color = (0.96, 1.0, 0.96) if is_correct else (1.0, 0.96, 0.96)
                text_color = (0.05, 0.40, 0.05) if is_correct else (0.75, 0.05, 0.05)

                page.draw_rect(rect, color=stroke_color, fill=fill_color, width=0.8 * scale)

                pad_x = 4.0 * scale
                pad_y = 3.0 * scale
                text_rect = fitz.Rect(rect.x0 + pad_x, rect.y0 + pad_y, rect.x1 - pad_x, rect.y1 - pad_y)

                rc = page.insert_textbox(
                    text_rect,
                    full_text,
                    fontsize=base_font,
                    fontname="helv",
                    color=text_color,
                    align=fitz.TEXT_ALIGN_LEFT
                )
                if rc < 0:
                    page.insert_textbox(
                        text_rect,
                        full_text,
                        fontsize=base_font * 0.85,
                        fontname="helv",
                        color=text_color,
                        align=fitz.TEXT_ALIGN_LEFT
                    )

                last_y_bottom = actual_y1

        out_pdf_bytes = doc.write()
        doc.close()

    st.success(f"Grading Complete! Overall Exam Score: **{total_exam_score} / 40**")
    st.download_button(
        label="📥 Download Annotated PDF",
        data=out_pdf_bytes,
        file_name=f"graded_{uploaded_student.name}",
        mime="application/pdf"
    )