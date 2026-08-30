# BOQ Parser — Construction Document AI

A production-grade PDF parsing system for construction BOQ (Bill of Quantities) reports, estimation sheets, stagewise material reports, and quantity surveys.

The API uses **PyMuPDF + Ollama (Llama 3.2)** as its primary extraction path. The existing **olmOCR** endpoint is used only when the primary path fails. The parser then applies layout analysis, table reconstruction, hierarchy detection, construction domain correction, mathematical validation, and multi-format export.

---

## Architecture

```
boqparser/
├── adapter/         OCRAdapter — Ollama primary and olmOCR fallback
├── extraction_service.py — PyMuPDF/Ollama and olmOCR services
├── pdf/             PDFProcessor — PyMuPDF metadata and rendering
├── layout/          LayoutAnalyzer — title/header/footer/table region detection
├── table/           TableReconstructor — HTML + coordinate-based BOQ row extraction
├── hierarchy/       HierarchyDetector — nested section tree (1 → 1.1 → 1.1.1)
├── domain/          DomainCorrector — 100+ construction terms + fuzzy correction
├── validation/      Validator — quantity × rate ≈ amount checks
├── export/          JSON, CSV, XLSX, Markdown, SQLite exporters
├── api/             FastAPI service
├── cli.py           Click CLI
└── parser.py        Main BOQParser orchestrator
```

### OCR Modes

| Mode | When | Accuracy |
|------|------|----------|
| **Primary (Ollama)** | Ollama running with `llama3.2` | Structures selectable BOQ text into JSON |
| **Fallback (olmOCR)** | Only when explicitly enabled with `--fallback` or `fallback=true` | Existing visual OCR endpoint |

---

## Quick Start

### 1. Install additional dependencies

```bash
pip install -r requirements_boqparser.txt
```

### 2. Start Ollama and install the model

```bash
ollama pull llama3.2
```

### 3. Parse a PDF (PyMuPDF + Ollama primary)

```bash
python -m boqparser.cli parse "Rev.Report 2 - BOQ.pdf" --output-dir ./output
```

### 4. Parse with olmOCR fallback only (requires running vLLM server)

```bash
python -m boqparser.cli parse "Rev.Report 2 - BOQ.pdf" \
  --fallback \
    --server-url http://localhost:8000/v1 \
    --output-dir ./output
```
### 5. Start the API server
### 4. Start the API server

```bash
python -m boqparser.cli serve --host 127.0.0.1 --port 8080
```

### 5. Use the API

```bash
# Health check
curl http://localhost:8080/health

# Parse and get JSON
curl -X POST http://localhost:8080/extract \
  -F "file=@Rev.Report_2_-_BOQ.pdf" \
  -F "fallback=true"

# Download XLSX
curl -X POST http://localhost:8080/extract/excel \
  -F "file=@Rev.Report_2_-_BOQ.pdf" \
  -F "fallback=true" \
  -o report.xlsx
```

---

## CLI Reference

```
boqparse parse <pdf>   --output-dir ./output --format json,csv,xlsx,md,db
                        --server-url http://localhost:8000/v1
                        --fallback           (olmOCR only)
                        --dpi 300            (minimum DPI)
                        --tolerance 0.02     (amount validation ± 2%)
                        --fuzzy-threshold 85 (domain correction sensitivity)

boqparse batch <dir>   --output-dir ./output --pattern "*.pdf"

boqparse serve          --host 0.0.0.0 --port 8080
                        --fallback
                        --server-url http://localhost:8000/v1

boqparse health         --server-url http://localhost:8000/v1
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Ollama primary and olmOCR fallback status |
| `/extract` | POST | Full parse result as JSON |
| `/extract/json` | POST | Download `report.json` |
| `/extract/csv` | POST | Download `report.csv` |
| `/extract/excel` | POST | Download `report.xlsx` |
| `/extract/markdown` | POST | Download `report.md` |

All `POST` endpoints accept `multipart/form-data` with:
- `file` (required): PDF upload
- `fallback` (bool, default false): Force olmOCR fallback mode
- `tolerance` (float, default 0.02): Amount validation tolerance

---

## Output Schema

### JSON (`report.json`)

```json
{
  "project_name": "PROPOSED RESIDENTIAL BUILDING",
  "report_type": "BOQ",
  "source_file": "/path/to/boq.pdf",
  "total_pages": 12,
  "pdf_type": "digital",
  "ocr_mode": "vlm",
  "sections": [
    {
      "section": "1 FOUNDATION WORKS",
      "number": "1",
      "title": "FOUNDATION WORKS",
      "depth": 0,
      "subtotal": 743643.25,
      "items": [
        {
          "item_no": "1.1.1",
          "description": "Earth Work for Footing",
          "quantity": 6035.75,
          "unit": "Cft",
          "rate": 11.0,
          "amount": 66393.25
        }
      ],
      "children": [...]
    }
  ],
  "validation": {
    "total_rows": 45,
    "valid_rows": 43,
    "invalid_rows": 2,
    "grand_total_computed": 2456789.50,
    "issues": [...]
  }
}
```

### CSV (`report.csv`)

```csv
section_path,item_no,description,quantity,unit,rate,amount,page_num
1 FOUNDATION WORKS/1.1 Earth Work,1.1.1,Earth Work for Footing,6035.75,Cft,11.0,66393.25,1
```

### SQLite (`report.db`)

Tables: `projects`, `sections`, `items`, `validation_issues`

---

## Validation

The validator checks:

1. **Amount consistency**: `|quantity × rate − amount| / max(amount, 1) ≤ tolerance`
2. **Section subtotals**: sum of item amounts ≈ declared subtotal
3. **Negative values**: flags negative quantity, rate, or amount
4. **Missing fields**: soft-flags rows with missing qty/rate/amount

Default tolerance: **2%** (configurable via `--tolerance`).

---

## Construction Domain Correction

The `DomainCorrector` module:
- Maintains 100+ construction terms (RCC, PCC, TMT, UPVC, Plastering, Footing, ...)
- Corrects known OCR errors: `Plastreing → Plastering`, `Winodw → Window`
- Normalises units: `sqft → Sqft`, `cft → Cft`, `nos → Nos.`, `mt → MT`
- Uses `rapidfuzz` for fuzzy matching (configurable threshold)

---

## Supported Document Types

- **BOQ Reports** (Bill of Quantities)
- **Stagewise Material Reports**
- **Construction Estimates**
- **Quantity Survey Reports**
- **Floorwise Material Summary Reports**

---

## Running Tests

```bash
# All boqparser tests (no vLLM required — runs in fallback mode)
python -m pytest tests/boqparser/ -v

# Specific test module
python -m pytest tests/boqparser/test_table_reconstructor.py -v
python -m pytest tests/boqparser/test_validator.py -v
python -m pytest tests/boqparser/test_exports.py -v
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API URL |
| `OLLAMA_MODEL` | `llama3.2` | Primary Ollama model |
| `OLLAMA_TIMEOUT` | `300` | Ollama request read timeout in seconds |
| `OLLAMA_CHUNK_SIZE` | `6000` | Maximum source characters per request; chunks end at line boundaries |
| `OLLAMA_MAX_RETRIES` | `1` | Retries for transient connection/server errors |
| `OLLAMA_MAX_OUTPUT_TOKENS` | `2048` | Maximum Llama output tokens per request |
| `OLLAMA_PAGE_CONCURRENCY` | `1` | Concurrent page requests sent to Ollama |
| `OLMOCR_SERVER_URL` | `http://localhost:8000/v1` | olmOCR fallback server URL |
| `BOQPARSER_DPI` | `300` | PDF rendering DPI |
| `BOQPARSER_TOLERANCE` | `0.02` | Amount validation tolerance |

---

## Python API

```python
from boqparser import BOQParser

# Parse in fallback mode (no VLM server needed)
parser = BOQParser(fallback_mode=True)
result = parser.parse("Rev.Report 2 - BOQ.pdf")

print(f"Project: {result.project_name}")
print(f"Grand Total: ₹{result.validation_summary.grand_total_computed:,.2f}")
print(f"Sections: {len(result.sections)}")
print(f"Items: {len(result.all_rows)}")

# Export to all formats
paths = result.save("./output", formats=["json", "csv", "xlsx", "md", "db"])

# Access structured data
for section in result.sections:
    print(f"\n{section.number} {section.title}")
    for item in section.items:
        print(f"  {item.item_no} | {item.description} | {item.quantity} {item.unit} | ₹{item.amount}")
```

---

## Project Structure

```
olmocr/               (existing olmocr repo — DO NOT MODIFY)
  olmocr/
    pipeline.py       Main olmocr VLM pipeline
    prompts/          VLM prompt builders
    data/renderpdf.py PDF → image rendering
    train/            Model training code

boqparser/            (NEW — this package)
  adapter/            OCRAdapter
  pdf/                PDFProcessor
  layout/             LayoutAnalyzer
  table/              TableReconstructor
  hierarchy/          HierarchyDetector
  domain/             DomainCorrector
  validation/         Validator
  export/             JSON/CSV/XLSX/MD/DB exporters
  api/                FastAPI service
  cli.py              CLI
  parser.py           Orchestrator

tests/boqparser/      Unit tests
requirements_boqparser.txt  Additional dependencies
README_BOQPARSER.md   This file
```
