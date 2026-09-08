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
MODEL = "gemini-3.6-flash"

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
    """Safely extracts and parses JSON even if wrapped in markdown fences or trailing thoughts."""
    text = raw_text.strip()
    
    # 1. Strip markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)
    
    # 2. Find the outermost JSON object bounds
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        candidate = text[start_idx:end_idx + 1]
    else:
        candidate = text

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # Fallback: clean trailing commas before closing braces/brackets
        fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
        return json.loads(fixed)
def grade_single_pdf(student_bytes: bytes, ms_b64: str) -> tuple[int, bytes]:
    student_b64 = base64.b64encode(student_bytes).decode("utf-8")
    doc = pymupdf.open(stream=student_bytes, filetype="pdf")
    total_pages = len(doc)

    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "application/pdf", "data": ms_b64}},
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
    last_error = ""

    for attempt in range(1, 4):
        gen_res = requests.post(target_url, json=payload, timeout=(15, 150))
        
        if gen_res.status_code == 200:
            try:
                res_json = gen_res.json()
                candidates = res_json.get("candidates", [])
                if not candidates:
                    raise RuntimeError("API returned 200 but candidate list is empty.")
                
                raw_text = candidates[0]["content"]["parts"][0]["text"]
                parsed_data = extract_and_parse_json(raw_text)
                
                pages_list = parsed_data.get("pages", [])
                results = {p.get("page_index", idx): p for idx, p in enumerate(pages_list)}
                success = True
                break
            except Exception as parse_err:
                last_error = f"Parse error on attempt {attempt}: {str(parse_err)}"
                if attempt < 3:
                    time.sleep(attempt * 2)
                    continue
        elif gen_res.status_code in (503, 429):
            last_error = f"Google busy ({gen_res.status_code})"
            time.sleep(attempt * 3)
        else:
            raise RuntimeError(f"API failed ({gen_res.status_code}): {gen_res.text}")

    if not success:
        raise RuntimeError(f"Failed to grade document. Details: {last_error}")

    total_exam_score = 0
    is_1_indexed = (0 not in results) and (1 in results)

    for i in range(total_pages):
        page = doc[i]
        w, h = page.rect.width, page.rect.height
        scale = max(1.0, w / 595.0)

        lookup_key = (i + 1) if is_1_indexed else i
        page_data = results.get(lookup_key, {"page_score": 0, "annotations": []})

        score = page_data.get("page_score", 0)
        max_marks = page_data.get("max_page_marks", "")
        total_exam_score += int(score) if str(score).isdigit() else 0

        # Score badge
        badge_w = 175.0 * scale
        badge_h = 28.0 * scale
        badge_margin = 12.0 * scale
        badge_rect = pymupdf.Rect(
            w - badge_w - badge_margin,
            badge_margin,
            w - badge_margin,
            badge_margin + badge_h
        )
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

        def get_y_percent(ann):
            if "y_position_percent" in ann:
                try:
                    return float(ann["y_position_percent"])
                except (ValueError, TypeError):
                    pass
            return 30.0

        annotations.sort(key=get_y_percent)

        margin_right = 10.0 * scale
        card_width = max(200.0 * scale, w * 0.32)
        x1 = w - margin_right
        x0 = max(10.0 * scale, x1 - card_width)
        base_font = 8.0 * scale
        usable_text_width = (x1 - x0) - (10.0 * scale)

        last_y_bottom = badge_margin + badge_h + (6.0 * scale)

        for item in annotations:
            is_correct = item.get("is_correct", False) or item.get("color") == "green" or "[1]" in item.get("comment", "")
            q_ref = item.get("question_ref", "")
            marks = item.get("awarded_marks", 1 if is_correct else 0)
            comment = item.get("comment", item.get("text", "")).strip()

            prefix = f"[+{marks}]" if is_correct else f"[{marks}]"
            tag = f"{q_ref} {prefix}".strip()
            full_text = sanitize_text(f"{tag}: {comment}" if tag else comment)

            line_len = pymupdf.get_text_length(full_text, fontname="helv", fontsize=base_font)
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

            rect = pymupdf.Rect(x0, actual_y0, x1, actual_y1)
            stroke_color = (0.15, 0.55, 0.15) if is_correct else (0.85, 0.15, 0.15)
            fill_color = (0.96, 1.0, 0.96) if is_correct else (1.0, 0.96, 0.96)
            text_color = (0.05, 0.40, 0.05) if is_correct else (0.75, 0.05, 0.05)

            page.draw_rect(rect, color=stroke_color, fill=fill_color, width=0.8 * scale)

            pad_x = 4.0 * scale
            pad_y = 3.0 * scale
            text_rect = pymupdf.Rect(rect.x0 + pad_x, rect.y0 + pad_y, rect.x1 - pad_x, rect.y1 - pad_y)

            rc = page.insert_textbox(
                text_rect,
                full_text,
                fontsize=base_font,
                fontname="helv",
                color=text_color,
                align=pymupdf.TEXT_ALIGN_LEFT
            )
            if rc < 0:
                page.insert_textbox(
                    text_rect,
                    full_text,
                    fontsize=base_font * 0.85,
                    fontname="helv",
                    color=text_color,
                    align=pymupdf.TEXT_ALIGN_LEFT
                )

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