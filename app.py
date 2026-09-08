import streamlit as st
import pymupdf
import requests
import json
import re
import os

# -------------------------------------------------------------
# CONFIGURATION & SECRETS
# -------------------------------------------------------------
API_KEY = st.secrets.get("GEMINI_API_KEY")
if not API_KEY:
    API_KEY = st.sidebar.text_input("Enter Gemini API Key", type="password")
    if not API_KEY:
        st.warning("Please configure your GEMINI_API_KEY in Streamlit Secrets or enter it in the sidebar.")
        st.stop()
MS_FILE_URI = "https://generativelanguage.googleapis.com/v1beta/files/64yo2okv35nq"
MODEL = "gemini-3.5-flash"

GENERATE_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
UPLOAD_URL = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={API_KEY}"

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
    text = re.sub(r"^```json\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"^```\s*", "", text, flags=re.MULTILINE)
    match = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()

def sanitize_text(text: str) -> str:
    replacements = {
        "✓": "[Y]", "✔": "[Y]", "✗": "[X]", "✘": "[X]",
        "±": "+/-", "°": " deg ", "→": "->", "Δ": "delta "
    }
    for k, v in replacements.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")

# -------------------------------------------------------------
# UI INTERFACE
# -------------------------------------------------------------
st.set_page_config(page_title="Paper 6 Chemistry Grader", layout="centered")
st.title("🧪 IGCSE Chemistry Paper 6 Grader")
st.caption("Upload student exam scans to automatically annotate and calculate total marks.")

uploaded_file = st.file_uploader("Upload Student PDF", type=["pdf"])

if uploaded_file and st.button("Start Grading", type="primary"):
    with st.spinner("Grading script against Cambridge mark scheme..."):
        pdf_bytes = uploaded_file.read()
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        total_pages = len(doc)

        # 1. Resumable Upload to Gemini File API
        data_len = str(len(pdf_bytes))
        headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": data_len,
            "X-Goog-Upload-Header-Content-Type": "application/pdf",
            "Content-Type": "application/json"
        }

        init = requests.post(
            UPLOAD_URL,
            headers=headers,
            json={"file": {"display_name": "student_paper_upload"}},
            timeout=(10, 30)
        )

        if init.status_code != 200:
            st.error(f"Failed to initiate PDF upload to Gemini ({init.status_code}): {init.text}")
            st.stop()

        session_url = init.headers.get("X-Goog-Upload-URL")
        if not session_url:
            st.error(f"Google did not provide an upload URL. Response: {init.text}")
            st.stop()

        upload_headers = {
            "Content-Length": data_len,
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize"
        }
        res = requests.post(session_url, headers=upload_headers, data=pdf_bytes, timeout=(10, 60))

        if res.status_code != 200:
            st.error(f"Failed to upload PDF data to Gemini ({res.status_code}): {res.text}")
            st.stop()

        file_info = res.json().get("file", {})
        student_uri = file_info.get("uri")
        student_file_name = file_info.get("name")

        if not student_uri:
            st.error("Could not obtain student file URI from Google API response.")
            st.stop()

        # 2. Query Gemini
        results = {}
        try:
            payload = {
                "contents": [{
                    "parts": [
                        {"file_data": {"mime_type": "application/pdf", "file_uri": MS_FILE_URI}},
                        {"file_data": {"mime_type": "application/pdf", "file_uri": student_uri}},
                        {"text": GRADING_PROMPT}
                    ]
                }],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.1,
                    "maxOutputTokens": 8192
                }
            }
            gen_res = requests.post(GENERATE_URL, json=payload, timeout=(10, 120))
            if gen_res.status_code == 200:
                raw_text = gen_res.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed_data = json.loads(clean_json(raw_text))
                pages_list = parsed_data.get("pages", [])
                results = {p.get("page_index", idx): p for idx, p in enumerate(pages_list)}
            else:
                st.error(f"Gemini API inference error ({gen_res.status_code}): {gen_res.text}")
                st.stop()
        except Exception as e:
            st.error(f"Error parsing model response: {e}")
            st.stop()
        finally:
            if student_file_name:
                try:
                    requests.delete(f"[https://generativelanguage.googleapis.com/v1beta/](https://generativelanguage.googleapis.com/v1beta/){student_file_name}?key={API_KEY}", timeout=10)
                except Exception:
                    pass

        # 3. Dynamic Box Annotation
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

            # Score badge in top-right
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

                if is_correct:
                    stroke_color = (0.15, 0.55, 0.15)
                    fill_color = (0.94, 0.98, 0.94)
                    text_color = (0.08, 0.40, 0.08)
                else:
                    stroke_color = (0.85, 0.15, 0.15)
                    fill_color = (1.0, 0.94, 0.94)
                    text_color = (0.70, 0.05, 0.05)

                page.draw_rect(rect, color=stroke_color, fill=fill_color, width=1.0 * scale)

                pad_x = 5.0 * scale
                pad_y = 4.0 * scale
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

        out_pdf_bytes = doc.write()
        doc.close()

    st.success(f"Grading Complete! Total Score: {total_exam_score} / 40")
    st.download_button(
        label="📥 Download Annotated PDF",
        data=out_pdf_bytes,
        file_name=f"graded_{uploaded_file.name}",
        mime="application/pdf"
    )