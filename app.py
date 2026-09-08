import streamlit as st
import pymupdf
import requests
import json
import re
import base64
import time

# -------------------------------------------------------------
# CONFIGURATION & SECRETS
# -------------------------------------------------------------
API_KEY = st.secrets.get("GEMINI_API_KEY", "")
MODEL = "gemini-3.6-flash"  # or your preferred flash model

if not API_KEY:
    API_KEY = st.sidebar.text_input("Enter Gemini API Key", type="password")
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

After you finish grading the exam, recheck again to make sure that there are no mistakes in checking paper 6 chemistry exam.

Make sure you add up the marks correctly, don't skip questions and make sure the paper is graded correctly based on the marking scheme and don't forget that there is error carried forward (ECF) in this exam so if the student makes a mistake in readings in a table and plots these incorrect plots correctly on the graph he only loses for incorrect reading in the table once.

OUTPUT FORMAT:
Output strictly valid JSON (no markdown fences, no ```json):
{
  "pages": [
    {
      "page_index": 0,
      "page_score": 6,
      "max_page_marks": 6,
      "annotations": [
        {
          "y_position_percent": 35,
          "question_ref": "4(a)",
          "awarded_marks": 1,
          "is_correct": true,
          "comment": "M1: Measured specific volume of water and recorded initial temp."
        }
      ]
    }
  ]
}
"""

def clean_json(text: str) -> str:
    text = text.strip()
    # Strip markdown code block fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    
    # Extract the outermost JSON object bounds
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

# -------------------------------------------------------------
# UI SETUP & SESSION STATE
# -------------------------------------------------------------
st.set_page_config(page_title="IGCSE Paper 6 Batch Grader", layout="centered")
st.title("🧪 IGCSE Chemistry Grader")

if "ms_base64" not in st.session_state:
    st.session_state.ms_base64 = None
if "ms_file_name" not in st.session_state:
    st.session_state.ms_file_name = None

# --- Step 1: Mark Scheme Upload ---
st.subheader("1. Mark Scheme (Upload Once)")
ms_upload = st.file_uploader("Upload Cambridge Mark Scheme (PDF)", type=["pdf"], key="ms_uploader")

if ms_upload is not None:
    st.session_state.ms_base64 = base64.b64encode(ms_upload.read()).decode("utf-8")
    st.session_state.ms_file_name = ms_upload.name
    st.success(f"Active Mark Scheme: **{st.session_state.ms_file_name}**")
elif st.session_state.ms_base64 is not None:
    st.info(f"Active Mark Scheme: **{st.session_state.ms_file_name}**")
    if st.button("Change Mark Scheme"):
        st.session_state.ms_base64 = None
        st.session_state.ms_file_name = None
        st.rerun()
else:
    st.warning("Please upload a Mark Scheme to begin grading.")
    st.stop()

# --- Step 2: Student Papers Upload ---
st.divider()
st.subheader("2. Student Submission")
uploaded_student = st.file_uploader("Upload Student PDF", type=["pdf"], key="student_uploader")

if uploaded_student and st.button("Grade Paper", type="primary"):
    with st.spinner("Grading script against session Mark Scheme..."):
        student_bytes = uploaded_student.read()
        student_base64 = base64.b64encode(student_bytes).decode("utf-8")
        doc = pymupdf.open(stream=student_bytes, filetype="pdf")
        total_pages = len(doc)

        payload = {
            "contents": [{
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": st.session_state.ms_base64
                        }
                    },
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": student_base64
                        }
                    },
                    {"text": GRADING_PROMPT}
                ]
            }],
            "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "pages": {
                        "type": "ARRAY",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "page_index": {"type": "INTEGER"},
                                "page_score": {"type": "NUMBER"},
                                "max_page_marks": {"type": "NUMBER"},
                                "annotations": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "y_position_percent": {"type": "NUMBER"},
                                            "question_ref": {"type": "STRING"},
                                            "awarded_marks": {"type": "NUMBER"},
                                            "is_correct": {"type": "BOOLEAN"},
                                            "comment": {"type": "STRING"}
                                        },
                                        "required": ["y_position_percent", "question_ref", "awarded_marks", "is_correct", "comment"]
                                    }
                                }
                            },
                            "required": ["page_index", "page_score", "annotations"]
                        }
                    }
                },
                "required": ["pages"]
            },
            "temperature": 0.1,
            "maxOutputTokens": 8192
        }
        }
        # Clean model and key strings to prevent whitespace/newline issues
        clean_model = str(MODEL).strip()
        clean_api_key = str(API_KEY).strip()

        target_url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={clean_key}"
        
        # --- INITIALIZE BEFORE LOOP ---
        results = {}
        success = False

        for attempt in range(1, 4):
            gen_res = requests.post(target_url, json=payload, timeout=(10, 120))
            if gen_res.status_code == 200:
                raw_text = gen_res.json()["candidates"][0]["content"]["parts"][0]["text"]
                try:
                    cleaned = clean_json(raw_text)
                    parsed_data = json.loads(cleaned)
                    pages_list = parsed_data.get("pages", [])
                    results = {p.get("page_index", idx): p for idx, p in enumerate(pages_list)}
                    success = True
                    break
                except json.JSONDecodeError as err:
                    if attempt < 3:
                        time.sleep(attempt * 2)
                        continue
                    st.error(f"Failed to parse model JSON: {err}")
                    with st.expander("Show raw model output for debugging"):
                        st.code(raw_text)
                    st.stop()
            elif gen_res.status_code in (503, 429):
                time.sleep(attempt * 3)
            else:
                st.error(f"Inference error ({gen_res.status_code}): {gen_res.text}")
                break

        if not success:
            st.error("Failed to grade submission after retries. Please submit again.")
            st.stop()
        # Dynamic Scale & Annotation
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
            max_marks = page_data.get("max_page_marks", "")
            total_exam_score += int(score) if str(score).isdigit() else 0

            # Badge
            badge_w = 175.0 * scale
            badge_h = 28.0 * scale
            badge_margin = 12.0 * scale
            badge_rect = pymupdf.Rect(w - badge_w - badge_margin, badge_margin, w - badge_margin, badge_margin + badge_h)
            page.draw_rect(badge_rect, color=(0.12, 0.45, 0.85), fill=(0.94, 0.97, 1.0), width=1.2 * scale)
            page.insert_textbox(badge_rect, f"Page Score: {score}" + (f" / {max_marks}" if max_marks else ""), fontsize=10.5 * scale, fontname="helv", color=(0.10, 0.35, 0.75), align=pymupdf.TEXT_ALIGN_CENTER)

            annotations = page_data.get("annotations", [])
            def get_y_percent(ann):
                if "y_position_percent" in ann:
                    return float(ann["y_position_percent"])
                if "box_2d" in ann and isinstance(ann["box_2d"], list) and len(ann["box_2d"]) == 4:
                    return float(ann["box_2d"][0]) / 10.0
                return 30.0

            annotations.sort(key=get_y_percent)

            last_y_bottom = (badge_margin + badge_h) + (10.0 * scale)
            margin_right = 14.0 * scale
            card_width = max(185.0 * scale, w * 0.28)
            x1 = w - margin_right
            x0 = max(10.0 * scale, x1 - card_width)
            base_font = 8.5 * scale
            usable_text_width = (x1 - x0) - (12.0 * scale)

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
                line_height = base_font * 1.3
                card_height = (est_lines * line_height) + (10.0 * scale)

                target_y = (get_y_percent(item) / 100.0) * h
                actual_y0 = max(target_y, last_y_bottom + (6.0 * scale))
                actual_y1 = actual_y0 + card_height

                bottom_limit = h - (15.0 * scale)
                if actual_y1 > bottom_limit:
                    actual_y0 = max(last_y_bottom + (2.0 * scale), bottom_limit - card_height)
                    actual_y1 = actual_y0 + card_height

                rect = pymupdf.Rect(x0, actual_y0, x1, actual_y1)
                color = (0.15, 0.55, 0.15) if is_correct else (0.85, 0.15, 0.15)
                fill = (0.94, 0.98, 0.94) if is_correct else (1.0, 0.94, 0.94)

                page.draw_rect(rect, color=color, fill=fill, width=1.0 * scale)
                pad_x, pad_y = 5.0 * scale, 4.0 * scale
                text_rect = pymupdf.Rect(rect.x0 + pad_x, rect.y0 + pad_y, rect.x1 - pad_x, rect.y1 - pad_y)

                rc = page.insert_textbox(text_rect, full_text, fontsize=base_font, fontname="helv", color=(0.08, 0.40, 0.08) if is_correct else (0.70, 0.05, 0.05), align=pymupdf.TEXT_ALIGN_LEFT)
                if rc < 0:
                    page.insert_textbox(text_rect, full_text, fontsize=base_font * 0.85, fontname="helv", color=(0.08, 0.40, 0.08) if is_correct else (0.70, 0.05, 0.05), align=pymupdf.TEXT_ALIGN_LEFT)

                last_y_bottom = actual_y1

        out_pdf_bytes = doc.write()
        doc.close()

    st.success(f"Grading Complete! Total Score: {total_exam_score} / 40")
    st.download_button(
        label="📥 Download Annotated PDF",
        data=out_pdf_bytes,
        file_name=f"graded_{uploaded_student.name}",
        mime="application/pdf"
    )