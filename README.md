# intoAEC-ocr: Local Layout-Independent Construction Document Extraction System

A lightweight, local, layout-independent extraction engine that converts digital construction PDFs—Bills of Quantities (BOQs), rate analyses, stagewise reports, and tender documents—into structured, validated JSON conforming to the authoritative `output.json` blueprint.

---

## 1. Core Capabilities & Architecture

1. **Native Vector PDF Extraction (Zero Heavy OCR / No GPU Required)**:
   - Streams native characters, word bounding boxes, and geometry directly using PyMuPDF (`fitz`).
   - Runs in < 500 MB RAM, perfectly adapted for machines with 8 GB RAM and 4 GB GPU VRAM.
   - Bypasses full-page raster image rendering and removes heavy vLLM server dependencies.

2. **Adaptive Structure & Column Discovery**:
   - Dynamically discovers table headers, column bounding intervals, and page running headers/footers without hardcoded templates.
   - Seamlessly handles reverse or non-standard column layouts (e.g. JBCC format where `Amount` precedes `Item No`, or `Rate` precedes `Quantity`).
   - Reconstructs multi-line item descriptions and preambles across page boundaries.

3. **Authoritative `output.json` Blueprint Contract**:
   Every extraction strictly complies with:
   ```json
   {
     "sections": [
       {
         "section_code": "0066",
         "section_title": "BILL NO. 1",
         "items": [
           {
             "item_no": "1",
             "description": "Excavation in earth...",
             "unit": "m3",
             "quantity": 140.0,
             "net_rate": 95.0,
             "gross_rate": 95.0,
             "amount": 13300.0,
             "materials": []
           }
         ]
       }
     ]
   }
   ```

4. **Bounded Local LLM Integration (Ollama)**:
   - **Recommended Model**: `qwen2.5:1.5b` (or `llama3.2:1b`).
   - **Why `qwen2.5:1.5b`**: Consumes only ~1.2 GB VRAM / RAM, provides fast JSON output, and excels at tabular schema disambiguation within hardware limits.
   - **Bounded Context**: Only header tokens (<500 tokens) are sent for semantic column role mapping. Never feeds raw 100-page document text to the LLM.
   - **Zero-Dependency Fallback**: 100% deterministic keyword and coordinate mapping operates seamlessly if Ollama is not installed or offline.

---

## 2. Installation & Requirements

Python 3.10+ is supported. Install dependencies:

```bash
pip install -r requirements.txt
# or for minimal parser dependencies:
pip install -r requirements_boqparser.txt
```

*(Optional)* If using Ollama for semantic header disambiguation:
1. Download Ollama from [ollama.com](https://ollama.com).
2. Pull the lightweight model:
   ```bash
   ollama pull qwen2.5:1.5b
   ```

---

## 3. How to Run

### Command-Line Interface (CLI)

Parse any construction PDF to JSON:
```bash
python -m boqparser.cli parse "test_documents/HOUSE-MALULEKE_BoQ Print.pdf" -o output.json
```

Parse specific pages:
```bash
python -m boqparser.cli parse "test_documents/HOUSE-MALULEKE_BoQ Print.pdf" --pages 32,33 -o output.json
```

Export to Excel (`.xlsx`) or CSV:
```bash
python -m boqparser.cli parse "test_documents/Rev.Report 2 - BOQ.pdf" -f excel -o boq.xlsx
```

Inspect document metadata and table headers:
```bash
python -m boqparser.cli inspect "test_documents/Rev.Report 5 - Rate analyis.pdf"
```

### Docker Deployment (One-Command on Any Laptop)

The entire application—including the **Ollama AI runtime**, **Qwen2.5 (1.5B) model**, and **Web Dashboard**—is fully containerized.

#### Option 1: Docker Compose (Recommended)
```bash
docker compose up -d
```
- Automatically builds the image, starts Ollama, verifies/pulls `qwen2.5:1.5b`, and starts the web service.
- Open **`http://localhost:8000`** in any browser.

#### Option 2: Docker Run
```bash
docker build -t intoaec-ocr .
docker run -d -p 8000:8000 -p 11434:11434 --name intoaec-ocr intoaec-ocr
```

---

### Local Python Execution

Start the local server directly:
```bash
python app.py
```
- Open **`http://localhost:8000`** in your browser.
- **REST Endpoints**:
  - `POST /extract`: Upload PDF and receive validated blueprint JSON.
  - `POST /extract/html`: Upload PDF and receive rendered HTML report.
  - `POST /extract/excel`: Download `.xlsx` spreadsheet.
  - `POST /api/save_json`: Save validated schema directly to project root `output.json`.
  - `GET /health`: Health and Ollama connectivity status.

---

## 4. Running Tests

The test suite includes 91 comprehensive unit, integration, and mathematical reconciliation tests:

```bash
pytest
```

