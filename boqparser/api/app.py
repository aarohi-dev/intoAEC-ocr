"""
FastAPI service for BOQ PDF parsing.

Endpoints:
  POST /extract            → full ParseResult JSON
  POST /extract/json       → download report.json
  POST /extract/csv        → download report.csv
  POST /extract/excel      → download report.xlsx
  POST /extract/markdown   → download report.md
  GET  /health             → server status
"""

from __future__ import annotations

import io
import logging
import os
import json
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse, HTMLResponse

from boqparser.parser import BOQParser

logger = logging.getLogger(__name__)

app = FastAPI(
    title="BOQ Parser API",
    description="Production-grade PDF parsing system for construction BOQ reports, "
                "powered by the olmocr VLM engine.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Shared parser instance (configured via env vars) ──────────────────────────

def _get_parser() -> BOQParser:
    return BOQParser(
        server_url=os.environ.get("OLMOCR_SERVER_URL", "http://localhost:8000/v1"),
        model_name=os.environ.get("OLMOCR_MODEL_NAME", "olmocr"),
        fallback_mode=os.environ.get("OLMOCR_FALLBACK", "1").lower() in ("1", "true", "yes"),
        dpi=int(os.environ.get("BOQPARSER_DPI", "300")),
        tolerance=float(os.environ.get("BOQPARSER_TOLERANCE", "0.02")),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _save_upload(upload: UploadFile) -> str:
    """Save uploaded file to a temp path and return path."""
    suffix = Path(upload.filename or "document.pdf").suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
        content = await upload.read()
        tf.write(content)
        return tf.name


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    """Serve the test interface."""
    static_file = Path(__file__).parent / "static" / "index.html"
    if static_file.exists():
        return HTMLResponse(content=static_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>BOQ Parser AI - UI not found</h1>")


@app.get("/health")
async def health():
    """Health check — tests olmocr and Ollama server connectivity."""
    parser = _get_parser()
    server_ok = parser.ocr_adapter.check_server()
    
    ollama_ok = False
    try:
        from boqparser.mapping.ollama_adapter import OllamaAdapter
        ollama_ok = OllamaAdapter().is_available()
    except Exception:
        pass
        
    return {
        "status": "ok",
        "olmocr_server": parser.ocr_adapter.server_url,
        "olmocr_server_reachable": server_ok,
        "ocr_mode": "vlm" if server_ok else "fallback (pypdfium2)",
        "ollama_connected": ollama_ok,
        "ollama_model": "qwen2.5:1.5b",
        "ollama_host": "http://localhost:11434",
        "version": "1.0.0",
    }


async def _run_extraction_task(
    task_id: str,
    pdf_path: str,
    fallback: bool,
    tolerance: float,
    dpi: int,
    server_url: Optional[str],
    use_llm: bool = True,
    ollama_model: str = "qwen2.5:1.5b",
    ollama_host: str = "http://localhost:11434"
):
    from boqparser.api.task_manager import TaskManager
    tm = TaskManager()
    try:
        pdf_hash = tm.get_pdf_hash(pdf_path)
        
        parser = BOQParser(
            server_url=server_url or os.environ.get("OLMOCR_SERVER_URL", "http://localhost:8000/v1"),
            fallback_mode=fallback,
            dpi=dpi,
            tolerance=tolerance,
            use_llm=use_llm,
            ollama_model=ollama_model,
            ollama_host=ollama_host,
            task_manager=tm,
            pdf_hash=pdf_hash,
        )
        
        tm.update_task_progress(task_id, 20, "processing")
        
        result = await parser.parse_async(pdf_path)
        
        tm.update_task_progress(task_id, 80, "processing")
        
        result_dict = result.to_dict()
        
        # Inject Pricing RAG recommendations
        try:
            from boqparser.domain.pricing_rag import PricingRAG
            rag = PricingRAG()
            
            all_items = result_dict.get("all_items", [])
            for item in all_items:
                desc = item.get("description", "")
                if desc:
                    item["suggested_rates"] = rag.recommend_rates(desc)
                    
            def inject_sections_rag(sections):
                for s in sections:
                    for item in s.get("items", []):
                        desc = item.get("description", "")
                        if desc:
                            item["suggested_rates"] = rag.recommend_rates(desc)
                    if s.get("children"):
                        inject_sections_rag(s["children"])
            inject_sections_rag(result_dict.get("sections", []))
        except Exception as rag_exc:
            logger.warning(f"Failed to inject RAG suggestions: {rag_exc}")
            
        tm.complete_task(task_id, result_dict)
        
        try:
            os.unlink(pdf_path)
        except Exception:
            pass
    except Exception as exc:
        logger.exception(f"Background task {task_id} failed: {exc}")
        tm.fail_task(task_id, str(exc))


@app.post("/extract")
async def extract(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="PDF file to parse"),
    server_url: Optional[str] = Form(None),
    fallback: bool = Form(False),
    tolerance: float = Form(0.02),
    dpi: int = Form(300),
    async_mode: bool = Form(False),
    use_llm: bool = Form(True),
    ollama_model: str = Form("qwen2.5:1.5b"),
    ollama_host: str = Form("http://localhost:11434"),
):
    """
    Parse a PDF. If async_mode is True, runs as a background task returning a task_id.
    Otherwise, runs synchronously and returns the full ParseResult JSON.
    """
    pdf_path = await _save_upload(file)
    
    if async_mode:
        import uuid
        task_id = str(uuid.uuid4())
        from boqparser.api.task_manager import TaskManager
        tm = TaskManager()
        tm.create_task(task_id, pdf_path)
        background_tasks.add_task(
            _run_extraction_task,
            task_id=task_id,
            pdf_path=pdf_path,
            fallback=fallback,
            tolerance=tolerance,
            dpi=dpi,
            server_url=server_url,
            use_llm=use_llm,
            ollama_model=ollama_model,
            ollama_host=ollama_host,
        )
        return JSONResponse(
            status_code=202,
            content={"task_id": task_id, "status": "processing", "progress": 0}
        )
    
    # Synchronous path
    try:
        from boqparser.api.task_manager import TaskManager
        tm = TaskManager()
        pdf_hash = tm.get_pdf_hash(pdf_path)
        
        parser = BOQParser(
            server_url=server_url or os.environ.get("OLMOCR_SERVER_URL", "http://localhost:8000/v1"),
            fallback_mode=fallback,
            dpi=dpi,
            tolerance=tolerance,
            use_llm=use_llm,
            ollama_model=ollama_model,
            ollama_host=ollama_host,
            task_manager=tm,
            pdf_hash=pdf_hash,
        )
        
        result = await parser.parse_async(pdf_path)
        result_dict = result.to_dict()
        
        # Inject Pricing RAG recommendations
        try:
            from boqparser.domain.pricing_rag import PricingRAG
            rag = PricingRAG()
            
            all_items = result_dict.get("all_items", [])
            for item in all_items:
                desc = item.get("description", "")
                if desc:
                    item["suggested_rates"] = rag.recommend_rates(desc)
                    
            def inject_sections_rag(sections):
                for s in sections:
                    for item in s.get("items", []):
                        desc = item.get("description", "")
                        if desc:
                            item["suggested_rates"] = rag.recommend_rates(desc)
                    if s.get("children"):
                        inject_sections_rag(s["children"])
            inject_sections_rag(result_dict.get("sections", []))
        except Exception as rag_exc:
            logger.warning(f"Failed to inject RAG suggestions: {rag_exc}")
            
        return JSONResponse(content=result_dict)
    except Exception as exc:
        logger.exception(f"/extract failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Poll background task status and progress."""
    from boqparser.api.task_manager import TaskManager
    tm = TaskManager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    if task["status"] == "completed":
        return JSONResponse(content=task["result"])
    elif task["status"] == "failed":
        return JSONResponse(
            status_code=500,
            content={"task_id": task_id, "status": "failed", "error": task["error"]}
        )
    else:
        return JSONResponse(
            content={"task_id": task_id, "status": "processing", "progress": task["progress"]}
        )


@app.post("/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    background_tasks: BackgroundTasks,
    server_url: Optional[str] = Form(None),
    fallback: bool = Form(False),
    tolerance: float = Form(0.02),
    dpi: int = Form(300),
):
    """Resume a failed or interrupted background parsing task."""
    from boqparser.api.task_manager import TaskManager
    tm = TaskManager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    pdf_path = task["pdf_path"]
    if not pdf_path or not Path(pdf_path).exists():
        raise HTTPException(status_code=400, detail="Source PDF file is missing on server, cannot resume")
        
    tm.update_task_progress(task_id, 0, "processing")
    
    background_tasks.add_task(
        _run_extraction_task,
        task_id=task_id,
        pdf_path=pdf_path,
        fallback=fallback,
        tolerance=tolerance,
        dpi=dpi,
        server_url=server_url
    )
    
    return JSONResponse(
        status_code=202,
        content={"task_id": task_id, "status": "processing", "progress": 0}
    )


@app.post("/api/save_json")
async def save_edited_json(request: Request):
    """
    Save manually edited blueprint JSON from frontend to output.json in the workspace root.
    """
    try:
        data = await request.json()
        from boqparser.schemas.output_schema import OutputSchemaValidator
        is_valid, errors = OutputSchemaValidator.validate_dict(data)
        
        workspace_root = Path(__file__).resolve().parents[2]
        out_file = workspace_root / "output.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            
        return JSONResponse(content={
            "status": "success",
            "valid": is_valid,
            "errors": errors if not is_valid else [],
            "saved_path": str(out_file),
            "message": "Successfully saved to output.json",
        })
    except Exception as e:
        logger.exception(f"save_edited_json failed: {e}")
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@app.post("/extract/json")
async def extract_json(
    file: UploadFile = File(...),
    fallback: bool = Form(False),
    tolerance: float = Form(0.02),
):
    """Parse PDF and return downloadable JSON file."""
    from boqparser.export import JSONExporter
    pdf_path = await _save_upload(file)
    try:
        parser = _get_parser()
        parser.ocr_adapter.fallback_mode = fallback
        parser.validator.tolerance = tolerance
        result = parser.parse(pdf_path)
        json_str = JSONExporter().to_string(result)
        return Response(
            content=json_str.encode("utf-8"),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{Path(pdf_path).stem}.json"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


@app.post("/extract/csv")
async def extract_csv(
    file: UploadFile = File(...),
    fallback: bool = Form(False),
):
    """Parse PDF and return downloadable CSV file."""
    from boqparser.export import CSVExporter
    pdf_path = await _save_upload(file)
    try:
        parser = _get_parser()
        parser.ocr_adapter.fallback_mode = fallback
        result = parser.parse(pdf_path)
        csv_str = CSVExporter().to_string(result)
        return Response(
            content=csv_str.encode("utf-8-sig"),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{Path(pdf_path).stem}.csv"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


@app.post("/extract/excel")
async def extract_excel(
    file: UploadFile = File(...),
    fallback: bool = Form(False),
):
    """Parse PDF and return downloadable XLSX file."""
    from boqparser.export import ExcelExporter
    pdf_path = await _save_upload(file)
    try:
        parser = _get_parser()
        parser.ocr_adapter.fallback_mode = fallback
        result = parser.parse(pdf_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tf:
            xlsx_path = tf.name

        ExcelExporter().export(result, xlsx_path)
        with open(xlsx_path, "rb") as f:
            xlsx_bytes = f.read()
        os.unlink(xlsx_path)

        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{Path(pdf_path).stem}.xlsx"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


@app.post("/extract/markdown")
async def extract_markdown(
    file: UploadFile = File(...),
    fallback: bool = Form(False),
):
    """Parse PDF and return downloadable Markdown file."""
    from boqparser.export import MarkdownExporter
    pdf_path = await _save_upload(file)
    try:
        parser = _get_parser()
        parser.ocr_adapter.fallback_mode = fallback
        result = parser.parse(pdf_path)
        md_str = MarkdownExporter().to_string(result)
        return Response(
            content=md_str.encode("utf-8"),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{Path(pdf_path).stem}.md"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


@app.post("/extract/html")
async def extract_html(
    file: UploadFile = File(...),
):
    """Parse PDF and return dynamic beautiful HTML report."""
    pdf_path = await _save_upload(file)
    try:
        html_content = generate_html_report_from_pdf(pdf_path)
        return HTMLResponse(content=html_content)
    except Exception as exc:
        logger.exception(f"/extract/html failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            os.unlink(pdf_path)
        except Exception:
            pass


def generate_html_report_from_pdf(pdf_path: str) -> str:
    import re
    import html
    import fitz
    from boqparser.parser_rate_analysis import RateAnalysisParser

    # If document is a standard BOQ or material report, render universal HTML table report
    if not RateAnalysisParser.detect(pdf_path):
        parser = _get_parser()
        result = parser.parse(pdf_path)
        title = html.escape(result.project_name or "Construction BOQ Report")
        rows_html = []
        for s in result.sections:
            s_title = html.escape(f"{s.section_code} {s.section_title}".strip())
            rows_html.append(f'<tr style="background:#1e293b; color:#93c5fd; font-weight:bold;"><td colspan="6">{s_title}</td></tr>')
            for it in s.items:
                if getattr(it, "is_header_row", False) or getattr(it, "is_subtotal_row", False):
                    continue
                qty = f"{it.quantity:,.2f}" if it.quantity is not None else "-"
                rate = f"₹{it.rate:,.2f}" if it.rate is not None else "-"
                amt = f"₹{it.amount:,.2f}" if it.amount is not None else "-"
                rows_html.append(
                    f'<tr>'
                    f'<td style="padding:8px; border:1px solid #334155;">{html.escape(str(it.item_no or ""))}</td>'
                    f'<td style="padding:8px; border:1px solid #334155;">{html.escape(str(it.description or ""))}</td>'
                    f'<td style="padding:8px; border:1px solid #334155; text-align:right;">{qty}</td>'
                    f'<td style="padding:8px; border:1px solid #334155; text-align:center;">{html.escape(str(it.unit or ""))}</td>'
                    f'<td style="padding:8px; border:1px solid #334155; text-align:right;">{rate}</td>'
                    f'<td style="padding:8px; border:1px solid #334155; text-align:right; font-weight:bold;">{amt}</td>'
                    f'</tr>'
                )

        total_computed = f"₹{result.validation_summary.grand_total_computed:,.2f}"
        return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background:#0f172a; color:#f8fafc; padding:2rem; }}
        table {{ width:100%; border-collapse:collapse; margin-top:1.5rem; background:#1e293b; border-radius:8px; overflow:hidden; }}
        th {{ background:#334155; color:#f1f5f9; padding:10px; text-align:left; border:1px solid #475569; }}
        td {{ border:1px solid #334155; }}
        .header {{ display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:1rem; }}
        .total-box {{ background:#1e293b; border:1px solid #3b82f6; padding:0.75rem 1.5rem; border-radius:6px; font-size:1.1rem; color:#60a5fa; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1 style="margin:0; font-size:1.5rem;">{title}</h1>
            <p style="margin:4px 0 0; color:#94a3b8; font-size:0.9rem;">Total Pages: {result.total_pages} | Extracted Items: {len(result.all_rows)}</p>
        </div>
        <div class="total-box">Grand Total: <strong>{total_computed}</strong></div>
    </div>
    <table>
        <thead>
            <tr><th>Item No</th><th>Description</th><th style="text-align:right;">Quantity</th><th style="text-align:center;">Unit</th><th style="text-align:right;">Rate</th><th style="text-align:right;">Amount</th></tr>
        </thead>
        <tbody>
            {"".join(rows_html)}
        </tbody>
    </table>
</body>
</html>"""

    UNIT_PAT = r'(?:Cft|CFT|Sft|SFT|SqFt|Sqft|Kg|KG|BAG|Nos|NOS|Ltr|LTR|LS|Ls|RFT|Rft|hr|Day|N/day|NO|m2|m3|Rm|rm|nos|litr|Litr)'

    WI_RE  = re.compile(
        r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+'
        r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
        r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s*(\S+)\s*$'
    )
    SEC_RE   = re.compile(r'^(\d{4})\s+(.+)$')
    GRP_RE   = re.compile(r'^Group\s*:\s*(\d+)\s*(.*)')
    RES_ID_RE = re.compile(r'^([A-Z][A-Z0-9]*-\d{2}-[A-Z0-9]+-?\d*(?:-\d+)?)')
    UNIT_RE  = re.compile(r'^(' + UNIT_PAT + r')\b\s*(.*)', re.I)
    NUMS_RE  = re.compile(r'[\d,]+\.?\d*')
    NOTE_RE  = re.compile(r'Forced gross price', re.I)

    doc = fitz.open(pdf_path)
    TOTAL_PAGES = len(doc)

    all_lines = []
    for pg_idx in range(TOTAL_PAGES):
        page = doc[pg_idx]
        words = page.get_text("words")
        if not words:
            continue
        rows = {}
        for w in words:
            y = round(w[1], 0)
            rows.setdefault(y, []).append((w[0], w[4]))
        for y in sorted(rows):
            line = " ".join(word for _, word in sorted(rows[y]))
            all_lines.append(line.strip())

    doc.close()

    SKIP_RE = re.compile(r'^(PAGE \d+ OF \d+|Aathiv builds and designs)$', re.I)
    HDR_WORDS = {"Resource ID", "Resource name", "Qty/W.I", "Net rate", "Gross rate", "Net Amount", "Gross Amount"}

    def is_hdr(ln):
        return sum(1 for k in HDR_WORDS if k in ln) >= 3

    lines = [ln for ln in all_lines if ln and not SKIP_RE.match(ln) and not is_hdr(ln)]

    def parse_resource_nums(nums):
        r = dict(qty_wi="", price="", net_unit="", gross_unit="", net_amt="", gross_amt="", pct="")
        if len(nums) > 0: r["qty_wi"]     = nums[0]
        if len(nums) > 1: r["price"]      = nums[1]
        if len(nums) > 2: r["net_amt"]    = nums[2]
        if len(nums) > 3: r["net_unit"]   = nums[3]
        if len(nums) > 5: r["gross_unit"] = nums[5]
        if len(nums) > 6: r["gross_amt"]  = nums[6]
        if len(nums) > 7: r["pct"]        = nums[7]
        return r

    tokens = []
    i = 0
    n = len(lines)

    while i < n:
        ln = lines[i]

        if ln.strip() == "Analysis of work items":
            tokens.append({"type": "doc_title"})
            i += 1; continue

        m = re.match(
            r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+(' + UNIT_PAT + r')\s+'
            r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
            r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s*$',
            ln, re.I
        )
        if m:
            tokens.append({"type": "work_item", "code": m.group(1), "number": m.group(2),
                            "description": m.group(3).strip(), "qty": m.group(4),
                            "unit": m.group(5), "net_rate": m.group(6), "gross_rate": m.group(7),
                            "amount": m.group(8), "pct": m.group(9).strip()})
            i += 1; continue

        m2 = re.match(
            r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+'
            r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
            r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s+(\S+)\s*$',
            ln
        )
        if m2:
            tokens.append({"type": "work_item", "code": m2.group(1), "number": m2.group(2),
                            "description": m2.group(3).strip(), "qty": m2.group(4),
                            "unit": m2.group(9), "net_rate": m2.group(5), "gross_rate": m2.group(6),
                            "amount": m2.group(7), "pct": m2.group(8).strip()})
            i += 1; continue

        m_start = re.match(r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+)', ln)
        if m_start:
            full = ln
            j = i + 1
            while j < n and j < i + 6:
                if re.search(r'\(Amt\.\)', full):
                    break
                nl = lines[j]
                if re.match(r'^(\d{4})\s+', nl) or re.match(r'^Group\s*:', nl):
                    break
                full += " " + nl
                j += 1

            if re.search(r'\(Amt\.\)', full):
                ma = re.match(
                    r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+(' + UNIT_PAT + r')\s+'
                    r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
                    r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s*$',
                    full, re.I
                )
                if ma:
                    tokens.append({"type": "work_item", "code": ma.group(1), "number": ma.group(2),
                                    "description": ma.group(3).strip(), "qty": ma.group(4),
                                    "unit": ma.group(5), "net_rate": ma.group(6), "gross_rate": ma.group(7),
                                    "amount": ma.group(8), "pct": ma.group(9).strip()})
                    i = j; continue

                mb = re.match(
                    r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+(.+?)\s+([\d,]+\.?\d*)\s+'
                    r'([\d,]+(?:\.[\d]+)?)\s*\(Net\)\s*([\d,]+(?:\.[\d]+)?)\s*\(Gross\)\s*'
                    r'([\d,]+(?:,[\d]+)*\.?\d*)\s*\(Amt\.\)\s*([\d.]+\s*%)\s+(\S+)\s*$',
                    full
                )
                if mb:
                    tokens.append({"type": "work_item", "code": mb.group(1), "number": mb.group(2),
                                    "description": mb.group(3).strip(), "qty": mb.group(4),
                                    "unit": mb.group(9), "net_rate": mb.group(5), "gross_rate": mb.group(6),
                                    "amount": mb.group(7), "pct": mb.group(8).strip()})
                    i = j; continue

        m_sec = re.match(r'^(\d{4})\s+(.+)$', ln)
        if m_sec and not re.search(r'\(Net\)|\(Amt\)', ln) and not re.match(r'^(\d{4})\s+([\d]+(?:\.[\d]+)+)\s+', ln):
            tokens.append({"type": "section", "code": m_sec.group(1), "title": m_sec.group(2).strip()})
            i += 1; continue

        m_grp = GRP_RE.match(ln)
        if m_grp:
            tokens.append({"type": "group", "num": m_grp.group(1), "name": m_grp.group(2).strip()})
            i += 1; continue

        m_rid = RES_ID_RE.match(ln)
        if m_rid:
            res_id = m_rid.group(1)
            rest   = ln[len(res_id):].strip()

            full_id = res_id
            j = i + 1
            if res_id.endswith('-') and j < n:
                nl = lines[j]
                m_tail = re.match(r'^(\d{3})\s*(.*)', nl)
                if m_tail:
                    full_id = res_id + m_tail.group(1)
                    tail_rest = m_tail.group(2).strip()
                    rest = (rest + " " + tail_rest).strip() if tail_rest else rest
                    j += 1

            unit_m = re.search(r'\b(' + UNIT_PAT + r')\b', rest, re.I)
            name = rest
            unit = ""
            nums = []

            if unit_m:
                unit = unit_m.group(1)
                name = rest[:unit_m.start()].strip()
                after = rest[unit_m.end():]
                nums = NUMS_RE.findall(after)
            else:
                name_parts = [rest] if rest else []
                while j < n and j < i + 6:
                    nl = lines[j]
                    if GRP_RE.match(nl) or re.match(r'^\d{4}\s', nl) or RES_ID_RE.match(nl):
                        break
                    um = UNIT_RE.match(nl)
                    if um:
                        unit = um.group(1)
                        nums = NUMS_RE.findall(um.group(2))
                        j += 1
                        break
                    if re.match(r'^[\d,]+\.?\d*(?:\s+[\d,]+\.?\d*)*$', nl):
                        nums = NUMS_RE.findall(nl)
                        j += 1
                        break
                    name_parts.append(nl)
                    j += 1
                name = " ".join(name_parts).strip()

            row = {"id": full_id, "name": name, "unit": unit}
            row.update(parse_resource_nums(nums))
            tokens.append({"type": "resource", "row": row})
            i = j; continue

        if NOTE_RE.search(ln):
            note = ln
            j = i + 1
            while j < n and j < i + 3:
                nl = lines[j]
                if re.match(r'^\d{4}|^Group', nl):
                    break
                note += " " + nl
                j += 1
            tokens.append({"type": "note", "text": note.strip()})
            i = j; continue

        i += 1

    WI_CNT  = sum(1 for t in tokens if t["type"] == "work_item")
    SEC_CNT = sum(1 for t in tokens if t["type"] == "section")
    RES_CNT = sum(1 for t in tokens if t["type"] == "resource")

    def esc(s):
        return html.escape(str(s), quote=True)

    CSS = """
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Roboto+Mono:wght@400;500&display=swap');
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    :root{
      --bg:#0d1117; --bg2:#121820; --bg3:#1a2130;
      --brd:#242d3e; --acc:#3b82f6; --acc2:#60a5fa;
      --gld:#f59e0b; --gld2:#fcd34d;
      --grn:#10b981; --grn2:#6ee7b7;
      --txt:#e2e8f0; --mu:#8b9ab3; --dim:#526175;
      --r:12px; --r2:8px;
    }
    body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--txt);line-height:1.55;font-size:14px}
    .nav{position:sticky;top:0;z-index:100;background:rgba(13,17,23,.93);backdrop-filter:blur(18px);
         border-bottom:1px solid var(--brd);display:flex;align-items:center;justify-content:space-between;padding:11px 28px}
    .nb{display:flex;align-items:center;gap:12px}
    .ni{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);
        display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:800;color:#fff;
        box-shadow:0 2px 12px rgba(59,130,246,.4)}
    .nn{font-size:14px;font-weight:700}
    .ns{font-size:11px;color:var(--mu)}
    .pills{display:flex;gap:8px}
    .pill{background:rgba(255,255,255,.05);border:1px solid var(--brd);border-radius:20px;
          padding:4px 12px;font-size:11px;color:var(--mu)}
    .hero{background:linear-gradient(160deg,#05080e 0%,#0a1525 50%,#07111c 100%);
          border-bottom:1px solid var(--brd);padding:58px 28px 50px;text-align:center;
          position:relative;overflow:hidden}
    .hero::before{content:'';position:absolute;inset:0;
      background:radial-gradient(ellipse 80% 40% at 50% -5%,rgba(59,130,246,.2),transparent);
      pointer-events:none}
    .htag{display:inline-block;background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.28);
          color:var(--acc2);font-size:11px;font-weight:600;letter-spacing:2.5px;text-transform:uppercase;
          padding:5px 16px;border-radius:20px;margin-bottom:18px;position:relative;z-index:1}
    h1.htitle{font-size:clamp(22px,4vw,40px);font-weight:800;
               background:linear-gradient(130deg,#fff 20%,#93c5fd 100%);
               -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
               margin-bottom:12px;position:relative;z-index:1}
    .hsub{color:var(--mu);font-size:15px;margin-bottom:36px;position:relative;z-index:1}
    .stats{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;position:relative;z-index:1}
    .stat{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:var(--r);
          padding:20px 26px;min-width:120px;backdrop-filter:blur(8px)}
    .sv{font-size:26px;font-weight:700;color:var(--acc2)}
    .sl{font-size:10px;color:var(--mu);text-transform:uppercase;letter-spacing:1.2px;margin-top:4px}
    .wrap{max-width:1400px;margin:0 auto;padding:28px 20px 80px}
    .smaj{display:flex;align-items:center;gap:14px;margin:36px 0 14px;padding:16px 22px;
          background:linear-gradient(135deg,#0a1c36,#071422);
          border:1px solid #1e3558;border-left:4px solid var(--acc);border-radius:var(--r);
          box-shadow:0 2px 16px rgba(0,0,0,.3)}
    .smin{display:flex;align-items:center;gap:12px;margin:20px 0 10px;padding:12px 18px;
          background:linear-gradient(135deg,#0b2214,#061409);
          border:1px solid #1a3d22;border-left:4px solid var(--grn);border-radius:var(--r)}
    .bmaj{background:var(--acc);color:#fff;font-size:10px;font-weight:700;
          padding:3px 9px;border-radius:5px;white-space:nowrap;font-family:'Roboto Mono',monospace}
    .bmin{background:var(--grn);color:#052;font-size:10px;font-weight:700;
          padding:3px 9px;border-radius:5px;white-space:nowrap;font-family:'Roboto Mono',monospace}
    .smaj-lbl{font-size:15px;font-weight:700;color:#93c5fd}
    .smin-lbl{font-size:13px;font-weight:600;color:var(--grn2)}
    .wi{background:linear-gradient(160deg,#111724,#0d1220);border:1px solid var(--brd);
        border-radius:var(--r);margin:12px 0;overflow:hidden;
        box-shadow:0 4px 20px rgba(0,0,0,.3);transition:border-color .18s,box-shadow .18s}
    .wi:hover{border-color:rgba(59,130,246,.3);box-shadow:0 8px 40px rgba(59,130,246,.1)}
    .wihd{background:linear-gradient(135deg,#152540,#0f1c30);padding:14px 20px;
          display:flex;align-items:center;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--brd)}
    .wcodes{display:flex;gap:6px;align-items:center;flex-shrink:0}
    .tc{background:rgba(59,130,246,.12);border:1px solid rgba(59,130,246,.28);color:var(--acc2);
        font-family:'Roboto Mono',monospace;font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px}
    .tn{background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.28);color:var(--gld2);
        font-family:'Roboto Mono',monospace;font-size:10px;font-weight:600;padding:2px 7px;border-radius:4px}
    .wdesc{flex:1;font-size:14px;font-weight:600;color:var(--txt);min-width:180px}
    .tp{background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.28);color:var(--grn2);
        font-size:11px;font-weight:600;padding:4px 12px;border-radius:20px;white-space:nowrap;flex-shrink:0}
    .wirates{display:grid;grid-template-columns:repeat(5,1fr);border-bottom:1px solid var(--brd)}
    @media(max-width:650px){.wirates{grid-template-columns:repeat(2,1fr)}}
    .rbox{padding:12px 14px;text-align:center;background:var(--bg2);border-right:1px solid var(--brd)}
    .rbox:last-child{border-right:none}
    .rlbl{font-size:9.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.7px;margin-bottom:5px}
    .rval{font-size:15px;font-weight:600;font-family:'Roboto Mono',monospace}
    .rv-qty{color:var(--txt)}
    .rv-unit{display:inline-block;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.22);
             color:var(--gld2);font-size:13px;padding:2px 12px;border-radius:14px}
    .rv-net{color:var(--acc2)}
    .rv-grs{color:var(--mu)}
    .rv-amt{color:var(--gld2);font-size:16px}
    .groups{padding:12px;display:flex;flex-direction:column;gap:10px}
    .gcard{background:var(--bg);border:1px solid var(--brd);border-radius:var(--r2);overflow:hidden}
    .ghd{background:var(--bg3);padding:8px 14px;display:flex;align-items:center;gap:10px;
         border-bottom:1px solid var(--brd)}
    .gnum{width:22px;height:22px;border-radius:50%;background:var(--acc);
          display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:#fff;flex-shrink:0}
    .gname{font-size:12px;font-weight:600;color:var(--acc2)}
    .twrap{overflow-x:auto}
    table.rt{width:100%;border-collapse:collapse;font-size:12px}
    table.rt th{background:#14294a;color:#93c5fd;font-size:10px;font-weight:600;
                text-transform:uppercase;letter-spacing:.5px;padding:8px 10px;
                white-space:nowrap;border-bottom:2px solid var(--brd)}
    table.rt th:nth-child(1),table.rt th:nth-child(2){text-align:left}
    table.rt th:nth-child(3){text-align:center}
    table.rt th:nth-child(n+4){text-align:right}
    table.rt td{padding:7px 10px;border-bottom:1px solid rgba(36,45,62,.7);
                font-family:'Roboto Mono',monospace;font-size:11px;text-align:right;color:var(--txt)}
    table.rt td:nth-child(1){text-align:left;white-space:nowrap}
    table.rt td:nth-child(2){text-align:left;font-family:'Inter',sans-serif;font-size:12px;color:var(--mu)}
    table.rt td:nth-child(3){text-align:center;color:var(--gld2)}
    table.rt tr:nth-child(even) td{background:#0e141f}
    table.rt tr:nth-child(odd) td{background:var(--bg2)}
    table.rt tr:hover td{background:rgba(59,130,246,.06)}
    .rid{font-family:'Roboto Mono',monospace;font-size:10px;color:var(--acc2);
         background:rgba(59,130,246,.07);padding:2px 5px;border-radius:4px;white-space:nowrap}
    .cnet{color:#86efac!important;font-weight:500}
    .cgrs{color:var(--gld2)!important;font-weight:600}
    .note{margin:6px 12px 4px;padding:8px 12px;
          background:rgba(245,158,11,.05);border:1px solid rgba(245,158,11,.18);
          border-left:3px solid var(--gld);border-radius:var(--r2);
          font-size:12px;color:var(--gld2);font-style:italic}
    footer{border-top:1px solid var(--brd);padding:24px 28px;text-align:center;font-size:12px;color:var(--dim)}
    .topbtn{position:fixed;bottom:22px;right:22px;width:42px;height:42px;border-radius:50%;
            background:var(--acc);border:none;cursor:pointer;display:flex;align-items:center;
            justify-content:center;box-shadow:0 4px 20px rgba(59,130,246,.45);
            transition:transform .18s,opacity .18s;opacity:0;pointer-events:none;z-index:200}
    .topbtn.show{opacity:1;pointer-events:auto}
    .topbtn:hover{transform:translateY(-3px)}
    """

    JS = """
    const b=document.querySelector('.topbtn');
    window.addEventListener('scroll',()=>b.classList.toggle('show',scrollY>300));
    b.addEventListener('click',()=>scrollTo({top:0,behavior:'smooth'}));
    """

    out = []
    out.append(f"""<!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>Rate Analysis Report &#8211; Aathiv Builds &amp; Designs</title>
    <meta name="description" content="Rate Analysis for Proposed Residential Building &#8211; Aathiv Builds &amp; Designs">
    <style>{CSS}</style>
    </head>
    <body>
    <nav class="nav">
      <div class="nb">
        <div class="ni">A</div>
        <div><div class="nn">Aathiv Builds &amp; Designs</div><div class="ns">Rate Analysis Report &mdash; Rev.&nbsp;5</div></div>
      </div>
      <div class="pills">
        <span class="pill">&#128196; {TOTAL_PAGES} Pages</span>
        <span class="pill">&#128203; {WI_CNT} Work Items</span>
        <span class="pill">&#127959; {SEC_CNT} Sections</span>
        <span class="pill">&#128290; {RES_CNT} Resources</span>
      </div>
    </nav>
    <div class="hero">
      <div class="htag">Aathiv Builds &amp; Designs</div>
      <h1 class="htitle">Analysis of Work Items</h1>
      <p class="hsub">Proposed Residential Building &mdash; Comprehensive Rate Analysis Report (Rev.&nbsp;5)</p>
      <div class="stats">
        <div class="stat"><div class="sv">{TOTAL_PAGES}</div><div class="sl">Total Pages</div></div>
        <div class="stat"><div class="sv">{WI_CNT}</div><div class="sl">Work Items</div></div>
        <div class="stat"><div class="sv">{SEC_CNT}</div><div class="sl">Sections</div></div>
        <div class="stat"><div class="sv">{RES_CNT}</div><div class="sl">Resources</div></div>
        <div class="stat"><div class="sv">&#8377; INR</div><div class="sl">Currency</div></div>
      </div>
    </div>
    <div class="wrap">""")

    in_wi  = False
    in_grp = False

    def close_g():
        nonlocal in_grp
        if in_grp:
            out.append("        </tbody></table></div>\n    </div>")
            in_grp = False

    def close_w():
        nonlocal in_wi
        close_g()
        if in_wi:
            out.append("  </div>\n</div>")
            in_wi = False

    for tok in tokens:
        tp = tok["type"]

        if tp == "doc_title":
            continue

        elif tp == "section":
            close_w()
            title = tok["title"]
            is_maj = (title == title.upper() and len(title) > 2) or tok["code"].endswith("00") or tok["code"].endswith("10")
            if is_maj:
                out.append(f'\n<div class="smaj"><span class="bmaj">{esc(tok["code"])}</span><span class="smaj-lbl">{esc(title)}</span></div>')
            else:
                out.append(f'\n<div class="smin"><span class="bmin">{esc(tok["code"])}</span><span class="smin-lbl">{esc(title)}</span></div>')

        elif tp == "work_item":
            close_w()
            in_wi = True
            out.append(f"""
    <div class="wi">
      <div class="wihd">
        <div class="wcodes"><span class="tc">{esc(tok["code"])}</span><span class="tn">{esc(tok["number"])}</span></div>
        <div class="wdesc">{esc(tok["description"])}</div>
        <span class="tp">{esc(tok["pct"])}</span>
      </div>
      <div class="wirates">
        <div class="rbox"><div class="rlbl">Quantity</div><div class="rval rv-qty">{esc(tok["qty"])}</div></div>
        <div class="rbox"><div class="rlbl">Unit</div><div class="rval"><span class="rv-unit">{esc(tok["unit"])}</span></div></div>
        <div class="rbox"><div class="rlbl">Net Rate / Unit (&#8377;)</div><div class="rval rv-net">{esc(tok["net_rate"])}</div></div>
        <div class="rbox"><div class="rlbl">Gross Rate / Unit (&#8377;)</div><div class="rval rv-grs">{esc(tok["gross_rate"])}</div></div>
        <div class="rbox"><div class="rlbl">Total Amount (&#8377;)</div><div class="rval rv-amt">{esc(tok["amount"])}</div></div>
      </div>
      <div class="groups">""")

        elif tp == "group":
            close_g()
            in_grp = True
            out.append(f"""
        <div class="gcard">
          <div class="ghd"><div class="gnum">{esc(tok["num"])}</div><div class="gname">{esc(tok["name"])}</div></div>
          <div class="twrap"><table class="rt">
            <thead><tr>
              <th>Resource ID</th><th>Resource Name</th><th>Unit</th>
              <th>Qty / W.I</th><th>Price</th>
              <th>Net Rate</th><th>Gross Rate</th>
              <th>Net Amount</th><th>Gross Amount</th><th>%</th>
            </tr></thead>
            <tbody>""")

        elif tp == "resource":
            if in_grp:
                r = tok["row"]
                out.append(f"""          <tr>
                <td><span class="rid">{esc(r.get("id",""))}</span></td>
                <td>{esc(r.get("name",""))}</td>
                <td>{esc(r.get("unit",""))}</td>
                <td>{esc(r.get("qty_wi",""))}</td>
                <td>{esc(r.get("price",""))}</td>
                <td>{esc(r.get("net_unit",""))}</td>
                <td>{esc(r.get("gross_unit",""))}</td>
                <td class="cnet">{esc(r.get("net_amt",""))}</td>
                <td class="cgrs">{esc(r.get("gross_amt",""))}</td>
                <td>{esc(r.get("pct",""))}</td>
              </tr>""")

        elif tp == "note":
            close_g()
            if in_wi:
                out.append(f'    <div class="note">&#8505; {esc(tok["text"])}</div>')

    close_w()

    out.append("""
    </div><!-- /wrap -->
    <footer>
      <p><strong>Aathiv Builds &amp; Designs</strong> &nbsp;&bull;&nbsp; Rate Analysis Report Rev.&nbsp;5 &nbsp;&bull;&nbsp; Proposed Residential Building</p>
      <p style="margin-top:6px;font-size:11px">All amounts in Indian Rupees (&#8377;). Generated from source PDF.</p>
    </footer>
    <button class="topbtn" title="Back to top">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
           stroke="white" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="18 15 12 9 6 15"/>
      </svg>
    </button>
    <script>""" + JS + """</script>
    </body>
    </html>""")

    return "\n".join(out)

# ── Entry point ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
