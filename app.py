# -*- coding: utf-8 -*-
import os
import json
import uuid
import textwrap
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import timedelta
import re

from flask import (
    Flask, render_template, request, jsonify, send_from_directory, session, abort
)
from werkzeug.utils import secure_filename

import fitz  # PyMuPDF
import langextract as lx

# ---------- Optional Docling dependencies ----------
_HAS_DOCLING = False
try:
    # Core converter and pipeline options
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    # Image output mode (placeholder / embedded / referenced)
    from docling_core.types.doc import ImageRefMode
    _HAS_DOCLING = True
except Exception:
    _HAS_DOCLING = False

# ---------------- Base configuration ----------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)
# Never hardcode third-party API keys here.
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-insecure-change-me")
app.config["MAX_CONTENT_LENGTH"] = 120 * 1024 * 1024
app.permanent_session_lifetime = timedelta(days=7)
ALLOWED_EXT = {"pdf"}
DEFAULT_PROMPT = textwrap.dedent("""\
    Extract the following entities: title, date, organization, person, email, phone, address, money.
    Rules:
    - `extraction_text` must come directly from the source text and must not be rewritten.
    - If a value can be normalized (for example `iso_date` or `currency/value`), place it in `attributes`.
    - Do not output content that cannot be located in the text.
""")
DEFAULT_EXAMPLE_TEXT = textwrap.dedent("""\
    EXAMPLES:
    INPUT:
    "Invoice #8821
    Billed To: Jane Q. Public
    Address: 12/45 Ocean View Rd, Bondi Beach NSW 2026
    Total: AUD 1,245.90
    Notes: Thanks!"
    OUTPUT:
    {"total_bill_amount":"AUD 1,245.90","payer_name":"Jane Q. Public","payer_address":"12/45 Ocean View Rd, Bondi Beach NSW 2026"}

    INPUT:
    "Receipt
    Customer: John Smith
    Amount Due: —
    Ship To: (none)"
    OUTPUT:
    {"total_bill_amount":null,"payer_name":"John Smith","payer_address":null}
""")

_INPUT_ANCHOR = re.compile(r'(?mi)^INPUT:\s*')
_OUTPUT_ANCHOR = re.compile(r'(?mi)^OUTPUT:\s*')

def _strip_wrapping_quotes(s: str) -> str:
    t = (s or "").strip()
    if len(t) >= 2 and ((t[0] == t[-1]) and t[0] in ('"', "'")):
        return t[1:-1]
    return t

def _find_first_json_object(s: str) -> Tuple[Dict[str, Any], int]:
    """
    Find the first JSON object inside string `s`, handling braces and escapes
    inside quoted strings.

    Returns `(parsed_obj, end_index_in_s)`. If nothing can be parsed, returns
    `(None, -1)`.
    """
    i = s.find('{')
    if i < 0:
        return None, -1
    depth = 0
    in_str = False
    esc = False
    for j in range(i, len(s)):
        ch = s[j]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[i:j+1])
                        return obj, j+1
                    except Exception:
                        break
    return None, -1

_MONEY_PATTERNS = [
    re.compile(r'^(?P<cur>[A-Z]{3})\s*(?P<num>[\d,]+(?:\.\d+)?)$'),
    re.compile(r'^(?P<sym>[$€¥￥])\s*(?P<num>[\d,]+(?:\.\d+)?)$'),
    re.compile(r'^(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<cur>[A-Z]{3}|\u5143|\u4eba\u6c11\u5e01)$'),
]

def _money_attrs(text: str) -> Dict[str, str]:
    t = (text or "").strip()
    if not t:
        return {}
    cur = None
    val = None
    for pat in _MONEY_PATTERNS:
        m = pat.match(t)
        if m:
            gd = m.groupdict()
            if gd.get("cur"):
                c = gd["cur"]
                if c in ("\u5143", "\u4eba\u6c11\u5e01"): cur = "CNY"
                else: cur = c
            elif gd.get("sym"):
                sym = gd["sym"]
                cur = {"$":"USD","€":"EUR","¥":"CNY","￥":"CNY"}.get(sym)
            if gd.get("num"):
                val = gd["num"].replace(",", "")
            break
    out = {}
    if cur: out["currency"] = cur
    if val: out["value"] = val
    return out

def build_examples_from_io_pairs(example_text: str) -> List[lx.data.ExampleData]:
    """
    Parse EXAMPLES / INPUT / OUTPUT text into LangExtract few-shot examples.

    Rules:
      - Only string values from OUTPUT JSON that can also be found in the
        matching INPUT text are converted into extractions.
      - Money-like keys (amount / total / price / bill) automatically receive
        `attributes.currency` and `attributes.value` when possible.
      - Returns `ExampleData(text=<clean input text>, extractions=[...])`.
    """
    s = example_text or ""
    pairs: List[Tuple[str, Dict[str, Any]]] = []

    # 1) Find all INPUT: blocks
    input_iters = list(_INPUT_ANCHOR.finditer(s))
    for idx, m_in in enumerate(input_iters):
        start_in = m_in.end()
        # The next INPUT: marks the upper bound for the current pair
        next_input_start = input_iters[idx+1].start() if idx+1 < len(input_iters) else len(s)

        # Find OUTPUT: inside the current window
        m_out = _OUTPUT_ANCHOR.search(s, pos=start_in, endpos=next_input_start)
        if not m_out:
            continue

        input_block = s[start_in:m_out.start()].strip()
        json_region = s[m_out.end():next_input_start].strip()
        # Remove wrapping quotes from INPUT
        input_clean = _strip_wrapping_quotes(input_block)

        obj, _ = _find_first_json_object(json_region)
        if obj is None or not isinstance(obj, dict):
            continue

        pairs.append((input_clean, obj))

    # 2) Convert pairs into ExampleData
    shots: List[lx.data.ExampleData] = []
    for inp, obj in pairs:
        exts = []
        for k, v in obj.items():
            if v is None:
                continue
            if isinstance(v, (int, float, bool)):
                # Prefer values that can still be matched in the source text
                v_str = str(v)
            elif isinstance(v, str):
                v_str = v
            else:
                continue

            if v_str and v_str in inp:
                attrs = {}
                k_low = k.lower()
                if any(t in k_low for t in ("amount","total","price","bill")):
                    attrs = _money_attrs(v_str) or {}
                exts.append(lx.data.Extraction(k, v_str, attributes=attrs or None))

        # Keep only examples that provide at least one valid extraction
        if exts:
            shots.append(lx.data.ExampleData(text=inp, extractions=exts))

    return shots
@app.before_request
def _make_session_permanent():
    session.permanent = True

def allowed_file(name: str) -> bool:
    return "." in name and name.rsplit(".", 1)[1].lower() in ALLOWED_EXT

# ---------------- Helper functions ----------------

def read_pdf_text_with_pymupdf(pdf_path: Path, ocr_if_empty: bool = False, ocr_dpi: int = 300) -> str:
    """Fallback implementation using PyMuPDF, with optional Tesseract OCR."""
    doc = fitz.open(pdf_path)
    parts: List[str] = []
    _has_ocr = False
    if ocr_if_empty:
        try:
            from PIL import Image  # noqa
            import pytesseract     # noqa
            _has_ocr = True
        except Exception:
            _has_ocr = False

    for i, page in enumerate(doc):
        txt = page.get_text("text") or ""
        if not txt.strip() and ocr_if_empty:
            if not _has_ocr:
                raise RuntimeError("This PDF appears to be image-based, but OCR dependencies are missing (Pillow, pytesseract, and system Tesseract).")
            from PIL import Image
            import pytesseract
            pix = page.get_pixmap(dpi=ocr_dpi)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            txt = pytesseract.image_to_string(img.convert("L"))
        if txt.strip():
            parts.append(f"[PAGE {i+1}]\n{txt.strip()}")
    return "\n\n".join(parts)

def _build_docling_pipeline_options(enable_ocr: bool, disable_table_structure: bool = False):
    pp = PdfPipelineOptions()
    # Increase render scale when better image quality is needed
    pp.images_scale = 2.0
    pp.generate_page_images = True
    pp.generate_picture_images = True
    # Some versions also support table images: pp.generate_table_images = True
    pp.do_ocr = bool(enable_ocr)
    pp.do_table_structure = not disable_table_structure
    return pp

def _is_docling_gl_compat_error(exc: Exception) -> bool:
    msg = str(exc or "")
    return "libGL.so.1" in msg or "cv2" in msg

def docling_convert(pdf_path: Path,
                    out_dir: Path,
                    enable_ocr: bool = False,
                    image_mode: str = "embedded") -> Dict[str, Any]:
    """
    Convert a PDF with Docling and export:
      - HTML (structured layout)
      - Markdown (better for LLM input)
      - Text (plain text)
      - JSON (lossless structure)

    Returns:
      { "html": "xxx.html", "md": "xxx.md", "txt": "xxx.txt",
        "json": "xxx.json", "md_text": "...", "text": "..." }
    """
    if not _HAS_DOCLING:
        raise RuntimeError("Docling is not installed. Please run `pip install docling` first.")

    compat_mode = False

    def _convert_with_options(pp):
        converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pp)}
        )
        return converter.convert(str(pdf_path))

    try:
        conv_res = _convert_with_options(
            _build_docling_pipeline_options(enable_ocr=enable_ocr)
        )
    except Exception as e:
        if not _is_docling_gl_compat_error(e):
            raise
        compat_mode = True
        conv_res = _convert_with_options(
            _build_docling_pipeline_options(
                enable_ocr=enable_ocr,
                disable_table_structure=True,
            )
        )

    doc = conv_res.document

    # 4) Choose image export mode
    mode = (image_mode or "embedded").strip().lower()
    if mode == "embedded":
        img_mode = ImageRefMode.EMBEDDED
        artifacts_dir = None
    elif mode == "referenced":
        img_mode = ImageRefMode.REFERENCED
        artifacts_dir = out_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
    else:  # "placeholder"
        img_mode = ImageRefMode.PLACEHOLDER
        artifacts_dir = None

    # 5) Export HTML / Markdown / JSON / Text and return the text content
    html_path = out_dir / "source.docling.html"
    md_path   = out_dir / "source.docling.md"
    json_path = out_dir / "source.docling.json"
    txt_path  = out_dir / "source.docling.txt"

    # Export through the DoclingDocument serializer
    doc.save_as_html(html_path, image_mode=img_mode, artifacts_dir=artifacts_dir)
    doc.save_as_markdown(md_path, image_mode=ImageRefMode.PLACEHOLDER)  # More token-efficient for LLM input
    doc.save_as_json(json_path)
    text_plain = doc.export_to_text()
    txt_path.write_text(text_plain, encoding="utf-8")

    md_text = md_path.read_text(encoding="utf-8")
    return {
        "html": html_path.name,
        "md": md_path.name,
        "json": json_path.name,
        "txt": txt_path.name,
        "md_text": md_text,
        "text": text_plain,
        "compat_mode": compat_mode,
    }

def normalize_to_iter(maybe_adocs):
    if hasattr(maybe_adocs, "documents"):
        return list(maybe_adocs.documents)
    if hasattr(maybe_adocs, "document_id"):
        return [maybe_adocs]
    try:
        return list(maybe_adocs)
    except TypeError:
        return [maybe_adocs]

def parse_jsonl(jsonl_path: Path) -> List[Dict[str, Any]]:
    out = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"raw": line})
    return out

def collect_extracted_texts(json_objs: List[Dict[str, Any]]) -> List[str]:
    texts = set()
    def likely_extraction(obj: Dict[str, Any]) -> Tuple[bool, str]:
        if not isinstance(obj, dict): return False, ""
        if "extraction_text" in obj and isinstance(obj["extraction_text"], str):
            return True, obj["extraction_text"]
        if "text" in obj and isinstance(obj["text"], str):
            if any(k in obj for k in ("label", "attributes", "span", "spans")):
                return True, obj["text"]
        return False, ""
    def walk(x):
        if isinstance(x, dict):
            ok, t = likely_extraction(x)
            if ok and t and len(t.strip()) >= 2:
                texts.add(t.strip())
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for it in x: walk(it)
    for item in json_objs: walk(item)
    return sorted(texts)

def export_highlighted_pdf(src_pdf: Path, terms: List[str], out_pdf: Path) -> int:
    doc = fitz.open(src_pdf)
    added = 0
    for page in doc:
        if not (page.get_text("text") or "").strip():
            continue
        for t in terms:
            try:
                rects = page.search_for(t)
            except Exception:
                rects = []
            for r in rects:
                page.add_highlight_annot(r); added += 1
    doc.save(out_pdf, garbage=4, deflate=True); doc.close()
    return added

def build_rows_for_table(json_objs: List[Dict[str, Any]], max_rows: int = 200):
    rows = []
    def push(label, text, attrs, page):
        rows.append({
            "label": label or "",
            "text": text or "",
            "attributes": json.dumps(attrs or {}, ensure_ascii=False),
            "page_hint": page
        })
    def walk(x, cur_page=None):
        if isinstance(x, dict):
            label = x.get("label") or x.get("entity")
            text  = x.get("extraction_text") or x.get("text")
            attrs = x.get("attributes") or {}
            page  = cur_page
            if "span" in x and isinstance(x["span"], dict):
                page = x["span"].get("page", page)
            if "spans" in x and isinstance(x["spans"], list) and x["spans"]:
                if isinstance(x["spans"][0], dict):
                    page = x["spans"][0].get("page", page)
            if label and text: push(label, text, attrs, page)
            for v in x.values(): walk(v, page)
        elif isinstance(x, list):
            for it in x: walk(it, cur_page)
    for item in json_objs: walk(item)
    seen, deduped = set(), []
    for r in rows:
        key = (r["label"], r["text"], r["attributes"], r["page_hint"])
        if key not in seen:
            seen.add(key); deduped.append(r)
        if len(deduped) >= max_rows: break
    return deduped

def ensure_api_key(req_api_key: str) -> str:
    """Prefer the request key, otherwise fall back to common environment variables."""
    if req_api_key and req_api_key.strip():
        return req_api_key.strip()
    for var in ("LANGEXTRACT_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        v = os.getenv(var)
        if v and v.strip():
            return v.strip()
    raise RuntimeError("No API key was detected. Enter one in the page or set LANGEXTRACT_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY / OPENAI_API_KEY.")

def run_extraction_job(
    *,
    job_id: str,
    prompt: str,
    model_id: str = "gemini-2.5-flash",
    api_key_in: str = "",
    enable_ocr: bool = False,
    export_hl: bool = False,
    use_example: bool = False,
    example_txt: str = "",
    img_mode_in: str = "embedded",
) -> Dict[str, Any]:
    """Run the full extraction pipeline and return the same payload used by the API."""
    if not (prompt or "").strip():
        raise ValueError("Please provide a prompt.")

    job_dir = RUNS_DIR / job_id
    pdf_path = job_dir / "source.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError("The uploaded PDF could not be found. Please upload it again.")

    api_key = ensure_api_key(api_key_in)
    docling_notice = None

    try:
        out_dir = job_dir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        dl = docling_convert(pdf_path, out_dir, enable_ocr=enable_ocr, image_mode=img_mode_in)
        input_text = dl["md_text"] or dl["text"]
        if dl.get("compat_mode"):
            docling_notice = "Docling switched to compatibility mode: table structure detection was disabled to work around missing libGL/cv2 dependencies in the current environment."
    except Exception as e:
        try:
            input_text = read_pdf_text_with_pymupdf(pdf_path, ocr_if_empty=enable_ocr)
            docling_notice = f"Docling conversion failed, so the app fell back to plain text via PyMuPDF: {e}"
        except Exception as e2:
            raise RuntimeError(f"Both Docling conversion and fallback text extraction failed: {e} | {e2}") from e2

    examples = []
    if use_example and example_txt:
        examples = build_examples_from_io_pairs(example_txt)

    def _flags_for_model(mid: str) -> Dict[str, Any]:
        m = (mid or "").lower()
        if m.startswith("gemini-"):
            return {}
        if m.startswith(("gpt-", "o", "chatgpt", "gpt-5")):
            return dict(fence_output=True, use_schema_constraints=False)
        return {}

    try:
        result = lx.extract(
            text_or_documents=input_text,
            prompt_description=prompt,
            examples=examples,
            model_id=model_id,
            api_key=api_key,
            **_flags_for_model(model_id)
        )
    except Exception as e:
        msg = str(e)
        if "Failed to parse" in msg or "JSONDecodeError" in msg:
            raise RuntimeError(
                "The model returned JSON that could not be parsed. Try `gemini-2.5-flash` (default) or `gpt-4o`. For OpenAI models, use `fence_output=True` and disable schema constraints."
            ) from e
        raise RuntimeError(f"LangExtract failed: {msg}") from e

    out_dir = job_dir / "out"
    adocs = normalize_to_iter(result)
    if not adocs:
        raise RuntimeError("LangExtract returned no results (`adocs` is empty).")

    jsonl_name = "extractions.jsonl"
    lx.io.save_annotated_documents(adocs, output_name=jsonl_name, output_dir=str(out_dir))

    html_content = lx.visualize(str(out_dir / jsonl_name))
    html_str = html_content.data if hasattr(html_content, "data") else html_content
    (out_dir / "visualization.html").write_text(html_str, encoding="utf-8")

    hl_pdf = None
    if export_hl:
        objs = parse_jsonl(out_dir / "extractions.jsonl")
        terms = collect_extracted_texts(objs)
        if terms:
            hl_path = out_dir / "highlighted.pdf"
            hits = export_highlighted_pdf(pdf_path, terms, hl_path)
            if hits > 0:
                hl_pdf = "highlighted.pdf"

    objs = parse_jsonl(out_dir / "extractions.jsonl")
    rows = build_rows_for_table(objs, max_rows=200)

    base = f"/files/{job_id}/"

    def build_download_url(filename: str) -> str | None:
        file_path = out_dir / filename
        return f"{base}{filename}" if file_path.exists() else None

    return {
        "job_id": job_id,
        "downloads": {
            "jsonl": base + "extractions.jsonl",
            "html": base + "visualization.html",
            "pdf": (base + hl_pdf) if hl_pdf else None,
            "docling_html": build_download_url("source.docling.html"),
            "docling_md": build_download_url("source.docling.md"),
            "docling_json": build_download_url("source.docling.json"),
            "docling_txt": build_download_url("source.docling.txt"),
        },
        "rows": rows,
        "total": len(objs),
        "viz_url": base + "visualization.html",
        "docling_url": build_download_url("source.docling.html"),
        "docling_notice": docling_notice,
        "out_dir": str(out_dir),
        "pdf_path": str(pdf_path),
    }

# ---------------- Page routes ----------------

@app.get("/")
def home():
    return render_template(
        "index.html",
        default_prompt=DEFAULT_PROMPT,
        default_example_text=DEFAULT_EXAMPLE_TEXT,
    )

# ---------------- API routes (AJAX, no full page reload) ----------------

@app.post("/api/upload")
def api_upload():
    """Upload a PDF, create a new job, and save the file as `source.pdf`."""
    file = request.files.get("pdf")
    if file is None or file.filename == "":
        return jsonify(ok=False, error="Please upload a PDF."), 400
    if not allowed_file(file.filename):
        return jsonify(ok=False, error="Only PDF files are supported."), 400

    job_id = uuid.uuid4().hex[:12]
    session["current_job_id"] = job_id
    job_dir = RUNS_DIR / job_id
    out_dir = job_dir / "out"
    job_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_name = secure_filename(file.filename)
    pdf_path = job_dir / "source.pdf"
    file.save(str(pdf_path))
    session["pdf_original_name"] = pdf_name

    return jsonify(ok=True, job_id=job_id, filename=pdf_name)

@app.post("/api/extract")
def api_extract():
    """Run Docling conversion and LangExtract extraction on an existing job PDF."""
    data = request.form if request.form else request.json or {}
    job_id = (data.get("job_id") or session.get("current_job_id") or "").strip()
    if not job_id:
        return jsonify(ok=False, error="No job was found. Please upload a PDF first."), 400

    prompt       = (data.get("prompt") or "").strip()
    model_id     = (data.get("model_id") or "gemini-2.5-flash").strip()
    api_key_in   = (data.get("api_key") or "").strip()
    enable_ocr   = (str(data.get("enable_ocr")).lower() in ("1","true","on","yes"))
    export_hl    = (str(data.get("export_hl_pdf")).lower() in ("1","true","on","yes"))
    use_example  = (str(data.get("use_example")).lower() in ("1","true","on","yes"))
    example_txt  = (data.get("example_text") or "").strip()
    img_mode_in  = (data.get("docling_image_mode") or "embedded").strip().lower()

    if not prompt:
        return jsonify(ok=False, error="Please provide a prompt."), 400

    job_dir = RUNS_DIR / job_id
    pdf_path = job_dir / "source.pdf"
    if not pdf_path.exists():
        return jsonify(ok=False, error="The uploaded PDF could not be found. Please upload it again."), 400

    try:
        resp = run_extraction_job(
            job_id=job_id,
            prompt=prompt,
            model_id=model_id,
            api_key_in=api_key_in,
            enable_ocr=enable_ocr,
            export_hl=export_hl,
            use_example=use_example,
            example_txt=example_txt,
            img_mode_in=img_mode_in,
        )
        return jsonify(ok=True, **resp)
    except (ValueError, FileNotFoundError) as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.get("/files/<job_id>/<path:filename>")
def serve_file(job_id, filename):
    job_dir = RUNS_DIR / job_id / "out"
    if not job_dir.exists():
        abort(404)
    return send_from_directory(job_dir, filename, as_attachment=False)

@app.post("/api/new_job")
def api_new_job():
    job_id = uuid.uuid4().hex[:12]
    session["current_job_id"] = job_id
    (RUNS_DIR / job_id / "out").mkdir(parents=True, exist_ok=True)
    return jsonify(ok=True, job_id=job_id)

@app.get("/api/self_test")
def api_self_test():
    demo_text = "ROMEO. But soft! What light through yonder window breaks?"
    prompt = "Extract entities: person. Return only structured results, and keep `extraction_text` exactly as it appears in the source text."
    examples = [lx.data.ExampleData(
        text=demo_text,
        extractions=[lx.data.Extraction("person", "ROMEO")]
    )]
    out = lx.extract(
        text_or_documents=demo_text,
        prompt_description=prompt,
        examples=examples,
        model_id="gemini-2.5-flash",
        api_key=ensure_api_key("")
    )
    return jsonify(ok=True, got=bool(out))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("FLASK_RUN_PORT", "8005")), debug=True)
