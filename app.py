import streamlit as st
import pymupdf
import requests
import json
import re
import base64
import time
import io
import zipfile

# -------------------------------------------------------------
# CONFIGURATION & SECRETS
# -------------------------------------------------------------
API_KEY = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
MODEL = "gemini-3.8-flash"

if not API_KEY:
    API_KEY = str(st.sidebar.text_input("Enter Gemini API Key", type="password")).strip()
    if not API_KEY:
        st.warning("Please configure your GEMINI_API_KEY in Streamlit Secrets or enter it in the sidebar.")
        st.stop()

GRADING_PROMPT = """
You are a senior Cambridge IGCSE Chemistry (0620 Paper 6) principal examiner.
Evaluate the complete student answer script against the official Cambridge Mark Scheme provided.
"If an answer area is completely blank, award [0] and add a concise note: 'Unattempted' without shifting subsequent question alignment"
"Ignore pre-existing red colored pen writing/ticks from previous checks and evaluate only the original blue or black student handwriting against the mark scheme"
If you find the word doubt written in a question, award [0] and add a concise note explaining the question and the answer in the marking scheme without shifting subsequent question alignment.

CORE INSTRUCTIONS:
1. STRICT MARK SCHEME ADHERENCE:
   - Award marks ONLY when the student satisfies the specific marking points (M1, A1, B1, etc.).
2. CROSSED-OUT WORK:
   - If work is crossed out and replaced: evaluate ONLY the replacement answer.
   - If work is crossed out but NOT replaced: grade the crossed-out work as valid.
3. PAGE NUMBERING CONVENTION:
   - You MUST use 0-based page indexing for the output JSON:
     * First physical page = page_index 0
     * Second physical page = page_index 1
     * Page N = page_index N-1
   - Never assign marks from one physical page to a different page index.
4. CRITICAL VERTICAL ALIGNMENT RULE:
   - For 'y_position_percent', provide the EXACT vertical percentage (from 0 to 100, where 0 is the top edge and 100 is the bottom edge) corresponding to WHERE THE STUDENT WROTE THEIR ANSWER for that sub-question.
   - DO NOT push annotations into the footer or generic coordinates. Align each box beside the dotted answer line or response region for that specific question.

After you finish grading the exam, recheck again to make sure that there are no mistakes in checking paper 6 chemistry exam.
Make sure you add up the marks correctly, don't skip questions and make sure the paper is graded correctly based on the marking scheme and don't forget that there is error carried forward (ECF) in this exam so if the student makes a mistake in readings in a table and plots these incorrect plots correctly on the graph he only loses for incorrect reading in the table once.
"""

def clean_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text

def sanitize_text(text: str) -> str:
    replacements = {
        "✓": "[Y]", "✔": "[Y]", "✗": "[X]", "✘": "[X]",
        "±": "+/-", "°": " deg ", "→": "->", "Δ": "delta "
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")

def extract_and_parse_json(raw_text: str) -> dict:
    """Parses the primary JSON structure and safely discards trailing text/objects."""
    text = raw_text.strip()
    
    # Strip Markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    
    # Find the opening brace of the JSON payload
    start_idx = text.find("{")
    if start_idx == -1:
        raise ValueError("No JSON object found in response.")
        
    text_to_parse = text[start_idx:]
    decoder = json.JSONDecoder()
    
    try:
        # raw_decode parses up to the exact end of the first valid JSON object
        obj, _ = decoder.raw_decode(text_to_parse)
        return obj
    except json.JSONDecodeError:
        # Secondary fallback: sanitize unescaped newlines or trailing commas
        cleaned = re.sub(r",\s*([\]}])", r"\1", text_to_parse)
        obj, _ = decoder.raw_decode(cleaned)
        return obj
PAGE_GRADING_PROMPT = """
You are a Cambridge IGCSE Chemistry (0620 Paper 6) examiner.
Evaluate this student answer page against the provided Mark Scheme.

RULES:
- If an answer area is completely blank, award [0] and comment: "Unattempted".
- Ignore pre-existing red ticks/notes; evaluate only blue/black handwriting.
- If "doubt" is written, award [0] with a brief explanation from the mark scheme.
- Award marks strictly according to Cambridge marking points (M1, A1, B1, etc.).
- Account for Error Carried Forward (ECF) where applicable.
- 'y_position_percent' MUST be the exact vertical coordinate (0 to 100) where the student wrote their response for that question.

Return strictly valid JSON:
{
  "page_score": 3,
  "max_page_marks": 5,
  "annotations": [
    {
      "y_position_percent": 42.5,
      "question_ref": "2(a)",
      "awarded_marks": 1,
      "is_correct": true,
      "comment": "M1: Correct observation recorded."
    }
  ]
}
"""

def grade_single_pdf(student_bytes: bytes, ms_b64: str) -> tuple[int, bytes]:
    doc = pymupdf.open(stream=student_bytes, filetype="pdf")
    total_pages = len(doc)
    total_exam_score = 0

    clean_model = str(MODEL).strip()
    clean_key = str(API_KEY).strip()
    target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={clean_key}"

    for i in range(total_pages):
        page = doc[i]
        w, h = page.rect.width, page.rect.height
        scale = max(1.0, w / 595.0)

        # Render only the current single page as a lightweight image
        pix = page.get_pixmap(dpi=130)
        img_bytes = pix.tobytes("jpeg")
        single_page_b64 = base64.b64encode(img_bytes).decode("utf-8")

        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "application/pdf", "data": ms_b64}},
                    {"inline_data": {"mime_type": "image/jpeg", "data": single_page_b64}},
                    {"text": f"{PAGE_GRADING_PROMPT}\nEvaluating page {i + 1} of {total_pages}."}
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
                "maxOutputTokens": 2048
            }
        }

        # Quick call per page: 25s timeout is plenty for 1 image
        page_data = {"page_score": 0, "max_page_marks": 0, "annotations": []}
        for attempt in range(1, 3):
            try:
                res = requests.post(target_url, json=payload, timeout=(10, 60))
                if res.status_code == 200:
                    raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    page_data = extract_and_parse_json(raw_text)
                    break
                elif res.status_code in (429, 503):
                    time.sleep(attempt * 4)
            except Exception:
                if attempt == 2:
                    break
                time.sleep(2)

        score = page_data.get("page_score", 0)
        max_marks = page_data.get("max_page_marks", "")
        total_exam_score += int(score) if str(score).isdigit() else 0

        # Score badge top right
        badge_w, badge_h, badge_margin = 175.0 * scale, 28.0 * scale, 12.0 * scale
        badge_rect = pymupdf.Rect(w - badge_w - badge_margin, badge_margin, w - badge_margin, badge_margin + badge_h)
        page.draw_rect(badge_rect, color=(0.12, 0.45, 0.85), fill=(0.94, 0.97, 1.0), width=1.2 * scale)
        page.insert_textbox(
            badge_rect,
            f"Page Score: {score}" + (f" / {max_marks}" if max_marks else ""),
            fontsize=10.5 * scale,
            fontname="helv",
            color=(0.10, 0.35, 0.75),
            align=pymupdf.TEXT_ALIGN_CENTER
        )

        annotations = page_data.get("annotations", [])
        annotations.sort(key=lambda a: float(a.get("y_position_percent", 30.0)))

        margin_right = 10.0 * scale
        card_width = max(200.0 * scale, w * 0.32)
        x1 = w - margin_right
        x0 = max(10.0 * scale, x1 - card_width)
        base_font = 8.0 * scale
        usable_text_width = (x1 - x0) - (10.0 * scale)
        last_y_bottom = badge_margin + badge_h + (6.0 * scale)

        for item in annotations:
            is_correct = item.get("is_correct", False) or "[1]" in item.get("comment", "")
            q_ref = item.get("question_ref", "")
            marks = item.get("awarded_marks", 1 if is_correct else 0)
            comment = item.get("comment", "").strip()

            prefix = f"[+{marks}]" if is_correct else f"[{marks}]"
            tag = f"{q_ref} {prefix}".strip()
            full_text = sanitize_text(f"{tag}: {comment}" if tag else comment)

            line_len = pymupdf.get_text_length(full_text, fontname="helv", fontsize=base_font)
            est_lines = max(1, int(line_len // usable_text_width) + full_text.count('\n') + 1)
            card_height = (est_lines * (base_font * 1.25)) + (8.0 * scale)

            target_y = (float(item.get("y_position_percent", 30.0)) / 100.0) * h
            actual_y0 = last_y_bottom + (3.0 * scale) if target_y < last_y_bottom else target_y
            actual_y1 = actual_y0 + card_height

            if actual_y1 > h - (10.0 * scale):
                actual_y1 = h - (10.0 * scale)
                actual_y0 = max(last_y_bottom, actual_y1 - card_height)

            rect = pymupdf.Rect(x0, actual_y0, x1, actual_y1)
            stroke_color = (0.15, 0.55, 0.15) if is_correct else (0.85, 0.15, 0.15)
            fill_color = (0.96, 1.0, 0.96) if is_correct else (1.0, 0.96, 0.96)
            text_color = (0.05, 0.40, 0.05) if is_correct else (0.75, 0.05, 0.05)

            page.draw_rect(rect, color=stroke_color, fill=fill_color, width=0.8 * scale)
            pad_x, pad_y = 4.0 * scale, 3.0 * scale
            text_rect = pymupdf.Rect(rect.x0 + pad_x, rect.y0 + pad_y, rect.x1 - pad_x, rect.y1 - pad_y)

            rc = page.insert_textbox(text_rect, full_text, fontsize=base_font, fontname="helv", color=text_color, align=pymupdf.TEXT_ALIGN_LEFT)
            if rc < 0:
                page.insert_textbox(text_rect, full_text, fontsize=base_font * 0.85, fontname="helv", color=text_color, align=pymupdf.TEXT_ALIGN_LEFT)

            last_y_bottom = actual_y1

    out_bytes = doc.write()
    doc.close()
    return total_exam_score, out_bytes
# -------------------------------------------------------------
# APPLICATION UI
# -------------------------------------------------------------
st.set_page_config(page_title="IGCSE Paper 6 Batch Grader", layout="centered")
st.title("🧪 IGCSE Chemistry Batch Grader")

if "ms_base64" not in st.session_state:
    st.session_state.ms_base64 = None
if "ms_file_name" not in st.session_state:
    st.session_state.ms_file_name = None

# Step 1: Mark Scheme Upload
st.subheader("1. Mark Scheme (Upload Once)")
ms_upload = st.file_uploader("Upload Cambridge Mark Scheme (PDF)", type=["pdf"], key="ms_uploader")

if ms_upload:
    st.session_state.ms_base64 = base64.b64encode(ms_upload.read()).decode("utf-8")
    st.session_state.ms_file_name = ms_upload.name
    st.success(f"Active Mark Scheme: **{st.session_state.ms_file_name}**")
elif st.session_state.ms_base64:
    st.info(f"Active Mark Scheme: **{st.session_state.ms_file_name}**")
    if st.button("Change Mark Scheme"):
        st.session_state.ms_base64 = None
        st.session_state.ms_file_name = None
        st.rerun()
else:
    st.warning("Please upload a Mark Scheme first to begin.")
    st.stop()

# Step 2: Student Papers Upload
st.divider()
st.subheader("2. Student Submissions")
uploaded_students = st.file_uploader(
    "Select student answer papers (PDF)",
    type=["pdf"],
    accept_multiple_files=True
)

if uploaded_students and st.button(f"Grade All ({len(uploaded_students)} Papers)", type="primary"):
    progress_bar = st.progress(0)
    status_text = st.empty()

    zip_buffer = io.BytesIO()
    summary_scores = []
    success_count = 0

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for idx, student_file in enumerate(uploaded_students):
            status_text.text(f"Grading {student_file.name} ({idx + 1}/{len(uploaded_students)})...")
            try:
                score, graded_bytes = grade_single_pdf(student_file.read(), st.session_state.ms_base64)
                out_name = f"Graded_{student_file.name}"
                zip_file.writestr(out_name, graded_bytes)
                summary_scores.append({"Student File": student_file.name, "Score": f"{score}/40", "Status": "Success"})
                success_count += 1
            except Exception as e:
                summary_scores.append({"Student File": student_file.name, "Score": "N/A", "Status": f"Failed: {str(e)}"})

            progress_bar.progress((idx + 1) / len(uploaded_students))
            
            # Wait 10-15 seconds between papers to prevent exhausting the TPM quota
            if idx < len(uploaded_students) - 1:
                status_text.text(f"Waiting 12s before starting next paper to clear API quota...")
                time.sleep(2)

    status_text.text("Batch processing complete!")
    st.table(summary_scores)

    if success_count > 0:
        zip_buffer.seek(0)
        st.download_button(
            label=f"📦 Download Graded Papers ({success_count} Papers in .ZIP)",
            data=zip_buffer,
            file_name="Graded_Submissions.zip",
            mime="application/zip"
        )
    else:
        st.error("No papers were successfully graded. Check the error messages in the table above.")