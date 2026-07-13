# BOQ Parser — Construction Document AI

A production-grade PDF parsing system for construction BOQ (Bill of Quantities) reports, estimation sheets, stagewise material reports, and quantity surveys.

Built on top of the **olmocr VLM OCR engine** (already present in this repository). The parser adds a complete document intelligence layer: PDF processing, layout analysis, table reconstruction, hierarchy detection, construction domain correction, mathematical validation, and multi-format export.

---

## Architecture

```
boqparser/
├── adapter/         OCRAdapter — wraps olmocr vLLM server or pypdfium2 fallback
├── pdf/             PDFProcessor — 300 DPI rendering via pypdfium2
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
| **VLM (olmocr)** | vLLM server running with olmOCR model | Highest — understands layout, tables, handwriting |
| **Fallback (pypdfium2)** | No server / `--fallback` flag | Good for digital PDFs with text layer |

---

## Quick Start

### 1. Install additional dependencies

```bash
pip install -r requirements_boqparser.txt
```

### 2. Parse a PDF (fallback mode — no GPU required)

```bash
python -m boqparser.cli parse "Rev.Report 2 - BOQ.pdf" --fallback --output-dir ./output
```

### 3. Parse with olmocr VLM (requires running vLLM server)

```bash
# Start the olmocr vLLM server first:
python -m olmocr.pipeline /path/to/workspace --model allenai/olmOCR-2-7B-1025-FP8

# Then parse:
python -m boqparser.cli parse "Rev.Report 2 - BOQ.pdf" \
    --server-url http://localhost:8000/v1 \
    --output-dir ./output
```

### 4. Start the API server

```bash
python -m boqparser.cli serve --port 8080 --fallback
# or with VLM:
python -m boqparser.cli serve --port 8080 --server-url http://localhost:8000/v1
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
                        --fallback           (no VLM, pypdfium2 only)
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
| `/health` | GET | Server status + VLM connectivity |
| `/extract` | POST | Full parse result as JSON |
| `/extract/json` | POST | Download `report.json` |
| `/extract/csv` | POST | Download `report.csv` |
| `/extract/excel` | POST | Download `report.xlsx` |
| `/extract/markdown` | POST | Download `report.md` |

All `POST` endpoints accept `multipart/form-data` with:
- `file` (required): PDF upload
- `fallback` (bool, default false): Force pypdfium2 mode
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
| `OLMOCR_SERVER_URL` | `http://localhost:8000/v1` | olmocr vLLM server URL |
| `OLMOCR_MODEL_NAME` | `olmocr` | Model name in vLLM |
| `OLMOCR_FALLBACK` | `0` | Set to `1` to force fallback mode |
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
