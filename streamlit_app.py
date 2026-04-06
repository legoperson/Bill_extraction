import uuid
from pathlib import Path

import pandas as pd
import streamlit as st

from app import DEFAULT_EXAMPLE_TEXT, DEFAULT_PROMPT, RUNS_DIR, run_extraction_job


st.set_page_config(page_title="AI Bill Extraction", layout="wide")
st.title("AI Bill Extraction and Highlighting")
st.caption("Upload a bill PDF, extract key fields, and highlight the matching text in the original file.")


def save_uploaded_pdf(uploaded_file) -> tuple[str, Path]:
    job_id = uuid.uuid4().hex[:12]
    job_dir = RUNS_DIR / job_id
    out_dir = job_dir / "out"
    job_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = job_dir / "source.pdf"
    pdf_path.write_bytes(uploaded_file.getbuffer())
    return job_id, pdf_path


with st.sidebar:
    st.header("Settings")
    model_id = st.selectbox(
        "Model",
        [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gpt-4o",
            "o4-mini",
            "gpt-4.1",
            "gpt-5",
        ],
        index=0,
    )
    api_key = st.text_input(
        "API Key",
        type="password",
        help="Leave blank to use LANGEXTRACT_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY from the environment.",
    )
    enable_ocr = st.checkbox("Enable OCR for scanned PDFs")
    export_hl = st.checkbox("Export highlighted PDF", value=True)
    image_mode = st.selectbox("Docling image mode", ["embedded", "referenced", "placeholder"], index=0)
    use_example = st.checkbox("Use few-shot examples")

uploaded_pdf = st.file_uploader("Upload a PDF bill", type=["pdf"])
prompt = st.text_area("Extraction prompt", value=DEFAULT_PROMPT, height=180)
example_text = st.text_area(
    "Few-shot examples",
    value=DEFAULT_EXAMPLE_TEXT,
    height=140,
    disabled=not use_example,
    help="Use INPUT:/OUTPUT: pairs if you want few-shot examples to be parsed.",
)

run_clicked = st.button("Run Extraction", type="primary", disabled=uploaded_pdf is None)

if run_clicked:
    if uploaded_pdf is None:
        st.error("Please upload a PDF first.")
    else:
        job_id, pdf_path = save_uploaded_pdf(uploaded_pdf)
        with st.spinner("Running Docling + extraction..."):
            try:
                result = run_extraction_job(
                    job_id=job_id,
                    prompt=prompt,
                    model_id=model_id,
                    api_key_in=api_key,
                    enable_ocr=enable_ocr,
                    export_hl=export_hl,
                    use_example=use_example,
                    example_txt=example_text,
                    img_mode_in=image_mode,
                )
                st.session_state["last_result"] = result
                st.session_state["last_pdf_path"] = str(pdf_path)
            except Exception as e:
                st.error(str(e))

result = st.session_state.get("last_result")
if result:
    out_dir = Path(result["out_dir"])
    st.success(f"Extraction finished for job `{result['job_id']}`.")
    if result.get("docling_notice"):
        st.warning(result["docling_notice"])

    col1, col2 = st.columns([1, 2])
    with col1:
        st.metric("Total records", result["total"])
        for label, filename in [
            ("Download JSONL", "extractions.jsonl"),
            ("Download Visualization HTML", "visualization.html"),
            ("Download Docling HTML", "source.docling.html"),
            ("Download Docling Markdown", "source.docling.md"),
            ("Download Docling JSON", "source.docling.json"),
            ("Download Docling Text", "source.docling.txt"),
            ("Download Highlighted PDF", "highlighted.pdf"),
        ]:
            file_path = out_dir / filename
            if file_path.exists():
                st.download_button(
                    label=label,
                    data=file_path.read_bytes(),
                    file_name=filename,
                    mime="application/octet-stream",
                    use_container_width=True,
                )

    with col2:
        rows = result.get("rows") or []
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No structured table rows were parsed. Download the JSONL file to inspect the raw output.")

    highlighted_pdf = out_dir / "highlighted.pdf"
    if highlighted_pdf.exists():
        st.subheader("Highlighted PDF Preview")
        st.download_button(
            label="Open highlighted PDF locally by downloading it",
            data=highlighted_pdf.read_bytes(),
            file_name=highlighted_pdf.name,
            mime="application/pdf",
        )
