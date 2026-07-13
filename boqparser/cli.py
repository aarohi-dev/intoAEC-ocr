"""
CLI interface for BOQ Parser.

Commands:
  parse    — Parse a single PDF
  batch    — Parse a directory of PDFs
  serve    — Start the FastAPI server
  health   — Check olmocr server connectivity
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("boqparser.cli")


# ─────────────────────────────────────────────────────────────────────────────
# CLI group
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option("1.0.0", prog_name="boqparse")
def cli():
    """BOQ Parser — Production-grade PDF parsing for construction documents.

    Uses the olmocr VLM engine (or pypdfium2 fallback) to extract structured
    data from BOQ reports, estimation sheets, and material summaries.
    """
    pass


# ─────────────────────────────────────────────────────────────────────────────
# parse command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--output-dir", "-o", default="./boq_output", show_default=True,
              help="Directory for output files.")
@click.option("--format", "-f", "formats",
              default="json,csv,xlsx,md,db", show_default=True,
              help="Comma-separated list of output formats: json,csv,xlsx,md,db")
@click.option("--server-url", default="http://localhost:8000/v1", show_default=True,
              help="olmocr vLLM server URL.")
@click.option("--fallback", is_flag=True, default=False,
              help="Force pypdfium2 fallback (no VLM server required).")
@click.option("--dpi", default=300, show_default=True, type=int,
              help="PDF rendering DPI (minimum 300).")
@click.option("--tolerance", default=0.02, show_default=True, type=float,
              help="Amount validation tolerance (relative error, e.g. 0.02 = 2%).")
@click.option("--fuzzy-threshold", default=85, show_default=True, type=int,
              help="Fuzzy match threshold for domain correction (0-100).")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Enable verbose logging.")
def parse(
    pdf_path: str,
    output_dir: str,
    formats: str,
    server_url: str,
    fallback: bool,
    dpi: int,
    tolerance: float,
    fuzzy_threshold: int,
    verbose: bool,
):
    """Parse a single BOQ PDF and export to multiple formats.

    PDF_PATH: Path to the PDF file to parse.
    """
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    fmt_list = [f.strip() for f in formats.split(",") if f.strip()]

    click.echo(f"Parsing: {pdf_path}")
    click.echo(f"   Server: {server_url} {'(fallback mode)' if fallback else ''}")
    click.echo(f"   DPI: {dpi} | Formats: {', '.join(fmt_list)}")
    click.echo()

    try:
        from boqparser.parser import BOQParser
        parser = BOQParser(
            server_url=server_url,
            fallback_mode=fallback,
            dpi=dpi,
            tolerance=tolerance,
            fuzzy_threshold=fuzzy_threshold,
        )
        result = parser.parse(pdf_path)
    except Exception as exc:
        click.echo(f"[ERROR] Parse failed: {exc}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    # Display summary
    vs = result.validation_summary
    click.echo(f"[SUCCESS] Parse complete!")
    click.echo(f"   Project: {result.project_name}")
    click.echo(f"   Type:    {result.report_type}")
    click.echo(f"   Pages:   {result.total_pages} ({result.pdf_type}, {result.ocr_mode} OCR)")
    click.echo(f"   Items:   {len(result.all_rows)} rows in {len(result.sections)} sections")
    click.echo(f"   Grand Total: Rs. {vs.grand_total_computed:,.2f}")
    if vs.invalid_rows:
        click.echo(f"   [WARNING] {vs.invalid_rows} validation issue(s)")

    # Save outputs
    click.echo(f"\nSaving to: {output_dir}")
    try:
        paths = result.save(output_dir, fmt_list)
        for fmt, path in paths.items():
            click.echo(f"   [{fmt.upper():5s}] {path}")
    except Exception as exc:
        click.echo(f"[ERROR] Export failed: {exc}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# batch command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("pdf_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--output-dir", "-o", default="./boq_output", show_default=True)
@click.option("--format", "-f", "formats", default="json,csv,xlsx,md,db", show_default=True)
@click.option("--server-url", default="http://localhost:8000/v1", show_default=True)
@click.option("--fallback", is_flag=True, default=False)
@click.option("--dpi", default=300, show_default=True, type=int)
@click.option("--tolerance", default=0.02, show_default=True, type=float)
@click.option("--pattern", default="*.pdf", show_default=True,
              help="Glob pattern for PDF files.")
def batch(
    pdf_dir: str,
    output_dir: str,
    formats: str,
    server_url: str,
    fallback: bool,
    dpi: int,
    tolerance: float,
    pattern: str,
):
    """Parse all PDFs in a directory.

    PDF_DIR: Directory containing PDF files.
    """
    pdf_files = sorted(Path(pdf_dir).glob(pattern))
    if not pdf_files:
        click.echo(f"[ERROR] No PDFs found matching '{pattern}' in {pdf_dir}", err=True)
        sys.exit(1)

    click.echo(f"Found {len(pdf_files)} PDF file(s) in {pdf_dir}")
    fmt_list = [f.strip() for f in formats.split(",") if f.strip()]

    from boqparser.parser import BOQParser
    parser = BOQParser(
        server_url=server_url,
        fallback_mode=fallback,
        dpi=dpi,
        tolerance=tolerance,
    )

    results = parser.parse_batch([str(p) for p in pdf_files], output_dir, fmt_list)

    click.echo("\nBatch Summary:")
    success = sum(1 for r in results if r["status"] == "success")
    click.echo(f"   Processed: {success}/{len(results)} successfully")

    for r in results:
        status = "[OK]" if r["status"] == "success" else "[ERR]"
        name = Path(r["pdf"]).name
        if r["status"] == "success":
            click.echo(
                f"   {status} {name} - {r.get('total_rows', 0)} rows, "
                f"Rs. {r.get('grand_total', 0):,.2f}"
            )
        else:
            click.echo(f"   {status} {name} - ERROR: {r.get('error', '?')}")

    # Save batch summary
    summary_path = Path(output_dir) / "batch_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    click.echo(f"\n   Summary saved: {summary_path}")


# ─────────────────────────────────────────────────────────────────────────────
# serve command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--server-url", default="http://localhost:8000/v1", show_default=True,
              help="olmocr vLLM server URL (set via env OLMOCR_SERVER_URL too).")
@click.option("--fallback", is_flag=True, default=False,
              help="Force pypdfium2 fallback mode (env: OLMOCR_FALLBACK=1).")
@click.option("--reload", is_flag=True, default=False,
              help="Auto-reload on code changes (development).")
def serve(host: str, port: int, server_url: str, fallback: bool, reload: bool):
    """Start the BOQ Parser FastAPI server."""
    os.environ.setdefault("OLMOCR_SERVER_URL", server_url)
    if fallback:
        os.environ["OLMOCR_FALLBACK"] = "1"

    try:
        import uvicorn
    except ImportError:
        click.echo("❌ uvicorn is required. pip install uvicorn[standard]", err=True)
        sys.exit(1)

    click.echo(f"Starting BOQ Parser API server at http://{host}:{port}")
    click.echo(f"   olmocr server: {server_url}")
    click.echo(f"   OCR mode: {'fallback (pypdfium2)' if fallback else 'VLM'}")
    click.echo(f"   Docs: http://{host}:{port}/docs")

    uvicorn.run(
        "boqparser.api.app:app",
        host=host,
        port=port,
        log_level="info",
        reload=reload,
    )


# ─────────────────────────────────────────────────────────────────────────────
# health command
# ─────────────────────────────────────────────────────────────────────────────

@cli.command()
@click.option("--server-url", default="http://localhost:8000/v1", show_default=True)
@click.option("--api-url", default="http://localhost:8080", show_default=True,
              help="BOQ Parser API URL (for checking the API service).")
def health(server_url: str, api_url: str):
    """Check connectivity of the olmocr vLLM server and BOQ Parser API."""
    from boqparser.adapter.ocr_adapter import OCRAdapter

    adapter = OCRAdapter(server_url=server_url)
    ok = adapter.check_server()

    click.echo("olmocr vLLM Server:")
    click.echo(f"  URL:        {server_url}")
    click.echo(f"  Reachable:  {'Yes' if ok else 'No (will use pypdfium2 fallback)'}")

    # Check BOQ Parser API
    try:
        import httpx
        resp = httpx.get(f"{api_url}/health", timeout=5.0)
        api_ok = resp.status_code == 200
        api_data = resp.json() if api_ok else {}
    except Exception:
        api_ok = False
        api_data = {}

    click.echo("\nBOQ Parser API:")
    click.echo(f"  URL:        {api_url}")
    click.echo(f"  Running:    {'Yes' if api_ok else 'No (start with: boqparse serve)'}")
    if api_ok:
        click.echo(f"  OCR mode:   {api_data.get('ocr_mode', '?')}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    cli()


if __name__ == "__main__":
    main()
