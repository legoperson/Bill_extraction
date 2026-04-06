# OCR_Agent

A Flask-based PDF extraction tool with support for:

- PDF text extraction via `PyMuPDF`
- structured HTML / Markdown / JSON / Text export via `Docling`
- field extraction via `LangExtract`
- optional OCR for scanned PDFs
- optional highlighted PDF export

The main entry point is `app.py`.

## 1. Python Dependencies

Install the Python packages first:

```bash
python -m pip install -r requirements.txt
```

`requirements.txt` currently contains:

```txt
Flask==2.2.5
PyMuPDF==1.27.2.2
langextract==1.2.0
docling==2.84.0
Pillow==10.2.0
pytesseract==0.3.13
```

Recommended Python version:

- Python `3.11`

## 2. System Dependencies

In addition to `pip` packages, this app needs a few system-level libraries.

### Amazon Linux 2023

If you deploy on Amazon Linux 2023, run:

```bash
sudo dnf install -y mesa-libGL tesseract tesseract-langpack-eng tesseract-langpack-chi_sim
```

Why these are needed:

- `mesa-libGL`: required by the `cv2` dependency used in the Docling stack on Linux
- `tesseract`: only needed when OCR is enabled
- `tesseract-langpack-eng` / `tesseract-langpack-chi_sim`: English and Simplified Chinese OCR language packs

### Ubuntu / Debian

For Ubuntu / Debian, the equivalent setup is:

```bash
sudo apt-get update
sudo apt-get install -y libgl1 tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim
```

## 3. API Key

You can enter an API key directly in the page, or set it as an environment variable.

Supported environment variables:

- `LANGEXTRACT_API_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_API_KEY`
- `OPENAI_API_KEY`

Example:

```bash
export LANGEXTRACT_API_KEY="your_api_key"
```

## 4. Run the App

Start the app from the `OCR_Agent` directory:

```bash
cd /home/ec2-user/UTS_WEB_SERVER/OCR_Agent
python app.py
```

Default port:

- `8005`

To override the port:

```bash
export FLASK_RUN_PORT=8005
python app.py
```

Then open:

```txt
http://<your-host>:8005
```

## 5. Feature Overview

### Core Features

These depend only on the Python packages listed in `requirements.txt`:

- PDF upload
- plain text extraction via PyMuPDF
- structured extraction via LangExtract
- JSONL output and HTML visualization

### Optional Features

These rely on additional system dependencies:

- Docling structured HTML preview
- OCR for scanned PDFs
- highlighted PDF export

## 6. Common Issues

### 1) `ModuleNotFoundError: No module named 'fitz'`

`PyMuPDF` is missing:

```bash
python -m pip install PyMuPDF
```

### 2) Docling preview is blank or `libGL.so.1` is missing

Your Linux environment is missing the OpenGL runtime library:

```bash
sudo dnf install -y mesa-libGL
```

Or on Ubuntu / Debian:

```bash
sudo apt-get install -y libgl1
```

### 3) OCR does not work / `tesseract` is not found

Install system Tesseract:

```bash
sudo dnf install -y tesseract tesseract-langpack-eng tesseract-langpack-chi_sim
```

### 4) Template or `runs` paths are incorrect after startup

The app now resolves these paths relative to `app.py`:

- `templates/`
- `static/`
- `runs/`

Make sure the entire `OCR_Agent` directory is copied together.

## 7. Suggested Deployment Steps

```bash
cd /home/ec2-user/UTS_WEB_SERVER/OCR_Agent
python -m pip install -r requirements.txt
sudo dnf install -y mesa-libGL tesseract tesseract-langpack-eng tesseract-langpack-chi_sim
export LANGEXTRACT_API_KEY="your_api_key"
python app.py
```

## 8. Notes

- `Docling` often requires additional low-level Linux libraries, and `mesa-libGL` is especially important
- If you only need basic extraction and do not use OCR or Docling preview, system dependency requirements are lighter
- For handoff or deployment, sending `requirements.txt` together with this `README.md` should be enough
