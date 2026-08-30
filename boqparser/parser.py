"""
BOQParser — Main orchestration pipeline.

Coordinates all modules:
  1. PDFProcessor   — render pages at 300 DPI
    2. OCRAdapter     — extract structured text (Ollama primary or olmOCR fallback)
  3. LayoutAnalyzer — classify page regions
  4. TableReconstructor — build BOQRows
  5. DomainCorrector — clean construction terminology
  6. HierarchyDetector — build section tree
  7. Validator      — check amount = qty × rate

Exports ParseResult which can be serialised by any Export module.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from boqparser.adapter.ocr_adapter import OCRAdapter, PageOCRResult
from boqparser.pdf.pdf_processor import PDFProcessor, PageImage
from boqparser.layout.layout_analyzer import LayoutAnalyzer, LayoutResult
from boqparser.table.table_reconstructor import TableReconstructor, BOQRow
from boqparser.hierarchy.hierarchy_detector import HierarchyDetector, Section
from boqparser.domain.domain_corrector import DomainCorrector
from boqparser.validation.validator import Validator, ValidationSummary

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ParseResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParseResult:
    """Complete result of parsing a BOQ PDF."""
    source_file: str
    project_name: str
    report_type: str
    pdf_type: str                          # 'digital', 'scanned', or 'mixed'
    ocr_mode: str                          # 'vlm' or 'fallback'
    total_pages: int
    sections: List[Section]
    all_rows: List[BOQRow]
    validation_summary: ValidationSummary
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        from boqparser.export.json_exporter import JSONExporter
        return JSONExporter._build_dict(self)

    def save(self, output_dir: str, formats: Optional[List[str]] = None) -> Dict[str, str]:
        """
        Save parse result to multiple formats.

        Args:
            output_dir: Directory to write output files.
            formats:    List of formats: 'json', 'csv', 'xlsx', 'md', 'db'.
                        Defaults to all.

        Returns:
            Dict mapping format → file path.
        """
        from boqparser.export import (
            JSONExporter, CSVExporter, ExcelExporter, MarkdownExporter, DBExporter
        )

        if formats is None:
            formats = ["json", "csv", "xlsx", "md", "db"]

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(self.source_file).stem

        paths: Dict[str, str] = {}

        if "json" in formats:
            p = JSONExporter().export(self, str(out_dir / f"{stem}.json"))
            paths["json"] = p

        if "csv" in formats:
            p = CSVExporter().export(self, str(out_dir / f"{stem}.csv"))
            paths["csv"] = p

        if "xlsx" in formats:
            try:
                p = ExcelExporter().export(self, str(out_dir / f"{stem}.xlsx"))
                paths["xlsx"] = p
            except ImportError as e:
                logger.warning(f"Excel export skipped: {e}")

        if "md" in formats:
            p = MarkdownExporter().export(self, str(out_dir / f"{stem}.md"))
            paths["md"] = p

        if "db" in formats:
            p = DBExporter().export(self, str(out_dir / f"{stem}.db"))
            paths["db"] = p

        return paths


# ─────────────────────────────────────────────────────────────────────────────
# Project name / report type detection
# ─────────────────────────────────────────────────────────────────────────────

_REPORT_TYPE_PATTERNS = {
    "BOQ": re.compile(r"\bBOQ\b|bill of quantities|bill of quantity", re.IGNORECASE),
    "Stagewise Report": re.compile(r"stage.?wise|stage report", re.IGNORECASE),
    "Material Report": re.compile(r"material.?report|material.?summary", re.IGNORECASE),
    "Estimation": re.compile(r"\bestimate\b|estimation|cost estimate", re.IGNORECASE),
    "Quantity Survey": re.compile(r"quantity.?survey|QS report", re.IGNORECASE),
}


def _detect_report_type(text: str) -> str:
    for rtype, pattern in _REPORT_TYPE_PATTERNS.items():
        if pattern.search(text):
            return rtype
    return "Construction Report"


def _extract_project_name(text: str, filename: str) -> str:
    """
    Try to extract a project name from the first few lines of OCR text.
    Falls back to the PDF filename.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # Look for a line with "PROJECT" or "PROPOSED" or ALL_CAPS title
    for line in lines[:15]:
        if re.search(r"project|proposed|building|residence|construction", line, re.IGNORECASE):
            if 5 < len(line) < 120:
                return line.title()
        if re.match(r"^[A-Z][A-Z\s\-,]+$", line) and 5 < len(line) < 80:
            return line.title()

    # Fall back to filename
    return Path(filename).stem.replace("_", " ").replace("-", " ").title()


# ─────────────────────────────────────────────────────────────────────────────
# BOQParser
# ─────────────────────────────────────────────────────────────────────────────

class BOQParser:
    """
    Production-grade BOQ PDF parser.

    Uses PyMuPDF and Ollama Llama 3.2, with olmOCR as the extraction fallback,
    data from construction BOQ PDFs.

    Args:
        server_url:       olmocr vLLM server URL. Default: http://localhost:8000/v1
        model_name:       Model name registered in the vLLM server.
        fallback_mode:    If True, use only olmOCR.
        dpi:              Rendering DPI (minimum 300).
        n_cols:           Expected number of columns in BOQ tables.
        tolerance:        Allowed relative error for amount validation.
        fuzzy_threshold:  Fuzzy matching threshold for domain correction.
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8000/v1",
        model_name: str = "olmocr",
        fallback_mode: bool = False,
        dpi: int = 300,
        n_cols: int = 6,
        tolerance: float = 0.02,
        fuzzy_threshold: int = 85,
        task_manager: Optional[Any] = None,
        pdf_hash: Optional[str] = None,
    ):
        self.ocr_adapter   = OCRAdapter(server_url, model_name, fallback_mode)
        self.pdf_processor = PDFProcessor(dpi=dpi)
        self.layout_analyzer = LayoutAnalyzer()
        self.table_reconstructor = TableReconstructor(n_cols=n_cols)
        self.hierarchy_detector  = HierarchyDetector()
        self.domain_corrector    = DomainCorrector(fuzzy_threshold=fuzzy_threshold)
        self.validator           = Validator(tolerance=tolerance)
        self.task_manager        = task_manager
        self.pdf_hash            = pdf_hash

    # ── main pipeline ─────────────────────────────────────────────────────────

    def parse(self, pdf_path: str) -> ParseResult:
        """
        Parse a BOQ PDF end-to-end.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            ParseResult with sections, rows, and validation summary.
        """
        pdf_path = str(Path(pdf_path).resolve())
        logger.info(f"BOQParser.parse(): {pdf_path}")

        # ── Step 1: PDF info ──────────────────────────────────────────────────
        total_pages = self.pdf_processor.get_page_count(pdf_path)
        pdf_type    = self.pdf_processor.detect_pdf_type(pdf_path)
        page_dims   = self.pdf_processor.get_page_dimensions(pdf_path)
        dim_tuples  = [(d["width_pts"], d["height_pts"]) for d in page_dims]

        logger.info(f"PDF: {total_pages} pages, type={pdf_type}")

        # ── Step 2: OCR all pages ─────────────────────────────────────────────
        ocr_results: List[PageOCRResult] = self.ocr_adapter.extract_all(pdf_path, total_pages, self.pdf_hash, self.task_manager)
        ocr_mode = "ollama" if any(r.ocr_mode == "ollama" for r in ocr_results) else "olmocr"

        # ── Step 3: Layout analysis ───────────────────────────────────────────
        layout_results: List[LayoutResult] = self.layout_analyzer.analyze_all(ocr_results, dim_tuples)

        # ── Step 4: Table reconstruction ──────────────────────────────────────
        all_rows: List[BOQRow] = self.table_reconstructor.reconstruct_all(ocr_results)

        # ── Step 5: Domain correction ─────────────────────────────────────────
        self.domain_corrector.correct_all(all_rows)

        # ── Step 6: Hierarchy detection ───────────────────────────────────────
        sections: List[Section] = self.hierarchy_detector.detect(all_rows)

        # ── Step 7: Validation ────────────────────────────────────────────────
        validation = self.validator.validate_all(all_rows, sections)

        # ── Metadata ──────────────────────────────────────────────────────────
        all_text = " ".join(r.raw_text for r in ocr_results)
        project_name = _extract_project_name(all_text, pdf_path)
        report_type  = _detect_report_type(all_text + " " + Path(pdf_path).name)

        metadata = {
            "total_pages": total_pages,
            "pdf_type": pdf_type,
            "ocr_mode": ocr_mode,
            "total_items": len(all_rows),
            "sections_found": len(sections),
            "validation_issues": validation.invalid_rows,
        }

        logger.info(
            f"Parse complete: {len(all_rows)} rows, {len(sections)} sections, "
            f"grand_total={validation.grand_total_computed}"
        )

        return ParseResult(
            source_file=pdf_path,
            project_name=project_name,
            report_type=report_type,
            pdf_type=pdf_type,
            ocr_mode=ocr_mode,
            total_pages=total_pages,
            sections=sections,
            all_rows=all_rows,
            validation_summary=validation,
            metadata=metadata,
        )

    async def parse_async(self, pdf_path: str) -> ParseResult:
        """
        Async version of parse() — uses async OCR extraction for concurrent page processing.
        """
        import asyncio
        pdf_path = str(Path(pdf_path).resolve())
        logger.info(f"BOQParser.parse_async(): {pdf_path}")

        total_pages = self.pdf_processor.get_page_count(pdf_path)
        pdf_type    = self.pdf_processor.detect_pdf_type(pdf_path)
        page_dims   = self.pdf_processor.get_page_dimensions(pdf_path)
        dim_tuples  = [(d["width_pts"], d["height_pts"]) for d in page_dims]

        # Async OCR
        ocr_results = await self.ocr_adapter.extract_all_async(pdf_path, total_pages, self.pdf_hash, self.task_manager)
        ocr_mode = "ollama" if any(r.ocr_mode == "ollama" for r in ocr_results) else "olmocr"

        layout_results = self.layout_analyzer.analyze_all(ocr_results, dim_tuples)
        all_rows = self.table_reconstructor.reconstruct_all(ocr_results)
        self.domain_corrector.correct_all(all_rows)
        sections = self.hierarchy_detector.detect(all_rows)
        validation = self.validator.validate_all(all_rows, sections)

        all_text = " ".join(r.raw_text for r in ocr_results)
        project_name = _extract_project_name(all_text, pdf_path)
        report_type  = _detect_report_type(all_text + " " + Path(pdf_path).name)

        metadata = {
            "total_pages": total_pages,
            "pdf_type": pdf_type,
            "ocr_mode": ocr_mode,
            "total_items": len(all_rows),
            "sections_found": len(sections),
            "validation_issues": validation.invalid_rows,
        }

        return ParseResult(
            source_file=pdf_path,
            project_name=project_name,
            report_type=report_type,
            pdf_type=pdf_type,
            ocr_mode=ocr_mode,
            total_pages=total_pages,
            sections=sections,
            all_rows=all_rows,
            validation_summary=validation,
            metadata=metadata,
        )

    def parse_batch(self, pdf_paths: List[str], output_dir: str, formats: Optional[List[str]] = None) -> List[Dict]:
        """
        Parse multiple PDFs and save outputs.

        Args:
            pdf_paths:  List of PDF file paths.
            output_dir: Directory to save outputs.
            formats:    Output formats (default all).

        Returns:
            List of dicts with 'pdf', 'status', 'output_files' keys.
        """
        results = []
        for pdf_path in pdf_paths:
            logger.info(f"Batch processing: {pdf_path}")
            try:
                result = self.parse(pdf_path)
                paths = result.save(output_dir, formats)
                results.append({
                    "pdf": pdf_path,
                    "status": "success",
                    "project_name": result.project_name,
                    "report_type": result.report_type,
                    "total_rows": len(result.all_rows),
                    "grand_total": result.validation_summary.grand_total_computed,
                    "output_files": paths,
                })
            except Exception as exc:
                logger.exception(f"Failed to parse {pdf_path}: {exc}")
                results.append({
                    "pdf": pdf_path,
                    "status": "error",
                    "error": str(exc),
                })

        return results
