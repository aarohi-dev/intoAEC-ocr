"""
BOQParser — Main unified orchestration pipeline for construction documents.

Coordinates:
  1. PDFProcessor       — streaming native vector text and geometry extraction
  2. StructureDiscovery — dynamic column & header alignment discovery
  3. TableReconstructor — adaptive line item & multi-line description reconstruction
  4. DomainCorrector    — clean construction terminology & normalize units
  5. HierarchyDetector  — build section and bill hierarchy
  6. Validator          — mathematical and schema verification

Outputs ParseResult strictly conforming to the authoritative output.json blueprint.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from boqparser.adapter.ocr_adapter import OCRAdapter, PageOCRResult
from boqparser.domain.domain_corrector import DomainCorrector
from boqparser.hierarchy.hierarchy_detector import HierarchyDetector, Section
from boqparser.layout.layout_analyzer import LayoutAnalyzer, LayoutResult
from boqparser.models.intermediate import CanonicalDocument, CanonicalSection
from boqparser.pdf.pdf_processor import PDFProcessor
from boqparser.pipeline.document_processor import DocumentProcessor
from boqparser.table.table_reconstructor import BOQRow, TableReconstructor
from boqparser.validation.validator import ValidationSummary, Validator

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ParseResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParseResult:
    """Complete result of parsing a construction PDF."""
    source_file: str
    project_name: str
    report_type: str
    pdf_type: str                          # 'digital', 'scanned', or 'mixed'
    ocr_mode: str                          # 'native'
    total_pages: int
    sections: List[Section]
    all_rows: List[BOQRow]
    validation_summary: ValidationSummary
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def grand_total_computed(self) -> float:
        return self.validation_summary.grand_total_computed if self.validation_summary else 0.0

    @property
    def grand_total_declared(self) -> Optional[float]:
        return self.validation_summary.grand_total_declared if self.validation_summary else None

    @property
    def reconciliation(self) -> Dict[str, Any]:
        return getattr(self.validation_summary, "reconciliation", {})

    def to_blueprint_dict(self) -> Dict[str, Any]:
        """Convert to the exact dictionary schema matching output.json."""
        from boqparser.export.json_exporter import JSONExporter
        return JSONExporter._build_blueprint_dict(self)

    def to_dict(self) -> Dict[str, Any]:
        """Returns blueprint dict along with top-level metadata for UI display."""
        d = self.to_blueprint_dict()
        # Attach top-level metadata for UI dashboard without corrupting the sections array
        d["project_name"] = self.project_name
        d["report_type"] = self.report_type
        d["total_pages"] = self.total_pages
        d["pdf_type"] = self.pdf_type
        d["ocr_mode"] = self.ocr_mode
        d["all_items"] = [row.to_dict() for row in self.all_rows if not getattr(row, "is_header_row", False) and not getattr(row, "is_subtotal_row", False)]
        d["validation"] = self.validation_summary.to_dict() if hasattr(self.validation_summary, "to_dict") else {}
        d["metadata"] = self.metadata
        return d

    def save(self, output_path: str, formats: Optional[List[str]] = None) -> Dict[str, str]:
        """Save parse result to multiple formats or a specific output file."""
        from boqparser.export import (
            JSONExporter, CSVExporter, ExcelExporter, MarkdownExporter, DBExporter
        )

        out_path = Path(output_path)
        paths: Dict[str, str] = {}
        stem = Path(self.source_file).stem

        # Check if output_path has a file extension (e.g. output.json, report.xlsx)
        suffix = out_path.suffix.lower().lstrip(".")
        if suffix in ("json", "csv", "xlsx", "md", "db"):
            out_path.parent.mkdir(parents=True, exist_ok=True)
            if suffix == "json":
                p = JSONExporter().export(self, str(out_path))
                paths["json"] = p
            elif suffix == "csv":
                p = CSVExporter().export(self, str(out_path))
                paths["csv"] = p
            elif suffix == "xlsx":
                p = ExcelExporter().export(self, str(out_path))
                paths["xlsx"] = p
            elif suffix == "md":
                p = MarkdownExporter().export(self, str(out_path))
                paths["md"] = p
            elif suffix == "db":
                p = DBExporter().export(self, str(out_path))
                paths["db"] = p
            return paths

        # Otherwise treat as output directory
        if formats is None:
            formats = ["json", "csv", "xlsx", "md", "db"]

        out_path.mkdir(parents=True, exist_ok=True)

        if "json" in formats:
            p = JSONExporter().export(self, str(out_path / f"{stem}.json"))
            paths["json"] = p

        if "csv" in formats:
            p = CSVExporter().export(self, str(out_path / f"{stem}.csv"))
            paths["csv"] = p

        if "xlsx" in formats:
            try:
                p = ExcelExporter().export(self, str(out_path / f"{stem}.xlsx"))
                paths["xlsx"] = p
            except ImportError as e:
                logger.warning(f"Excel export skipped: {e}")

        if "md" in formats:
            p = MarkdownExporter().export(self, str(out_path / f"{stem}.md"))
            paths["md"] = p

        if "db" in formats:
            p = DBExporter().export(self, str(out_path / f"{stem}.db"))
            paths["db"] = p

        return paths


def _extract_project_name(text: str, filename: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in lines[:15]:
        if re.search(r"project|proposed|building|residence|construction", line, re.IGNORECASE):
            if 5 < len(line) < 120:
                return line.title()
    return Path(filename).stem.replace("_", " ").replace("-", " ").title()


# ─────────────────────────────────────────────────────────────────────────────
# BOQParser Facade
# ─────────────────────────────────────────────────────────────────────────────

class BOQParser:
    """
    Unified local, layout-independent construction document parser.
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8000/v1",
        model_name: str = "olmocr",
        fallback_mode: bool = True,
        dpi: int = 300,
        n_cols: int = 6,
        tolerance: float = 0.02,
        fuzzy_threshold: int = 85,
        use_llm: bool = True,
        ollama_model: str = "qwen2.5:1.5b",
        ollama_host: str = "http://localhost:11434",
        task_manager: Optional[Any] = None,
        pdf_hash: Optional[str] = None,
    ):
        self.ollama_adapter = None
        if use_llm:
            from boqparser.mapping.ollama_adapter import OllamaAdapter
            adapter = OllamaAdapter(base_url=ollama_host, model=ollama_model)
            if adapter.is_available():
                self.ollama_adapter = adapter
                logger.info(f"Ollama connected: {ollama_host} (model: {ollama_model})")
            else:
                logger.info("Ollama server not reachable. Running in deterministic heuristic mode.")

        self.ocr_adapter = OCRAdapter(server_url, model_name, fallback_mode=True)
        self.pdf_processor = PDFProcessor(dpi=dpi)
        self.layout_analyzer = LayoutAnalyzer()
        self.table_reconstructor = TableReconstructor(n_cols=n_cols, ollama_adapter=self.ollama_adapter)
        self.hierarchy_detector = HierarchyDetector()
        self.domain_corrector = DomainCorrector(fuzzy_threshold=fuzzy_threshold)
        self.validator = Validator(tolerance=tolerance)
        self.task_manager = task_manager
        self.pdf_hash = pdf_hash
        self.document_processor = DocumentProcessor(
            tolerance=tolerance,
            fuzzy_threshold=fuzzy_threshold,
            ollama_adapter=self.ollama_adapter,
        )

    def parse(self, pdf_path: str, pages: Optional[List[int]] = None) -> ParseResult:
        """Parse a construction PDF end-to-end natively."""
        pdf_path = str(Path(pdf_path).resolve())
        logger.info(f"BOQParser.parse(): {pdf_path}")

        # Check for specialized Rate Analysis format
        from boqparser.parser_rate_analysis import RateAnalysisParser
        if RateAnalysisParser.detect(pdf_path):
            logger.info("Rate Analysis format detected. Using RateAnalysisParser.")
            ra_parser = RateAnalysisParser(tolerance=self.validator.tolerance)
            sections, all_rows = ra_parser.parse(pdf_path, pages=pages)

            validation = self.validator.validate_all(all_rows, sections)
            all_text = " ".join(row.description for row in all_rows)
            project_name = _extract_project_name(all_text, pdf_path)

            metadata = {
                "total_pages": self.pdf_processor.get_page_count(pdf_path),
                "pdf_type": self.pdf_processor.detect_pdf_type(pdf_path),
                "ocr_mode": "native",
                "total_items": len(all_rows),
                "sections_found": len(sections),
                "validation_issues": validation.invalid_rows,
            }

            return ParseResult(
                source_file=pdf_path,
                project_name=project_name,
                report_type="Rate Analysis Report",
                pdf_type=metadata["pdf_type"],
                ocr_mode="native",
                total_pages=metadata["total_pages"],
                sections=sections,
                all_rows=all_rows,
                validation_summary=validation,
                metadata=metadata,
            )

        # Standard / Universal native streaming path
        canonical_doc = self.document_processor.process_document(pdf_path, pages=pages)

        # Convert CanonicalSection objects to Section for backwards compatibility
        if canonical_doc.metadata.get("tree_sections"):
            sections: List[Section] = canonical_doc.metadata["tree_sections"]
            all_rows: List[BOQRow] = []
            for s in sections:
                all_rows.extend(s.all_items())
        else:
            sections: List[Section] = []
            all_rows: List[BOQRow] = []
            for c_sec in canonical_doc.sections:
                sec_items: List[BOQRow] = []
                for it in c_sec.items:
                    row = BOQRow(
                        item_no=it.item_no,
                        description=it.description,
                        quantity=it.quantity,
                        unit=it.unit,
                        rate=it.net_rate,
                        net_rate=it.net_rate,
                        gross_rate=it.gross_rate,
                        amount=it.amount,
                        section_code=c_sec.section_code,
                        section_title=c_sec.section_title,
                        materials=[m.to_dict() if hasattr(m, "to_dict") else m for m in it.materials],
                    )
                    sec_items.append(row)
                    all_rows.append(row)

                sec = Section(
                    section_code=c_sec.section_code,
                    section_title=c_sec.section_title,
                    number=c_sec.section_code,
                    title=c_sec.section_title,
                    items=sec_items,
                    subtotal=c_sec.subtotal,
                )
                sections.append(sec)

        validation = self.validator.validate_all(all_rows, sections)
        if canonical_doc.metadata.get("reconciliation"):
            validation.reconciliation = canonical_doc.metadata["reconciliation"]
            validation.grand_total_declared = canonical_doc.metadata.get("grand_total_declared")
            validation.reconciliation_valid = canonical_doc.metadata.get("reconciliation_valid", True)
        if canonical_doc.metadata.get("grand_total_computed"):
            validation.grand_total_computed = canonical_doc.metadata["grand_total_computed"]

        project_name = _extract_project_name(Path(pdf_path).name, pdf_path)

        metadata = {
            "total_pages": canonical_doc.total_pages,
            "pdf_type": self.pdf_processor.detect_pdf_type(pdf_path),
            "ocr_mode": "native",
            "total_items": len(all_rows),
            "sections_found": len(sections),
            "validation_issues": validation.invalid_rows,
            "grand_total": canonical_doc.metadata.get("grand_total") or validation.grand_total_declared or validation.grand_total_computed,
            "grand_total_computed": canonical_doc.metadata.get("grand_total_computed") or validation.grand_total_computed,
            "grand_total_declared": canonical_doc.metadata.get("grand_total_declared") or validation.grand_total_declared,
            "reconciliation": canonical_doc.metadata.get("reconciliation") or validation.reconciliation,
            "reconciliation_valid": canonical_doc.metadata.get("reconciliation_valid") if "reconciliation_valid" in canonical_doc.metadata else validation.reconciliation_valid,
        }

        return ParseResult(
            source_file=pdf_path,
            project_name=project_name,
            report_type="Bill of Quantities",
            pdf_type=metadata["pdf_type"],
            ocr_mode="native",
            total_pages=canonical_doc.total_pages,
            sections=sections,
            all_rows=all_rows,
            validation_summary=validation,
            metadata=metadata,
        )

    async def parse_async(self, pdf_path: str) -> ParseResult:
        """Async wrapper around parse()."""
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.parse, pdf_path)

    def parse_batch(self, pdf_paths: List[str], output_dir: str, formats: Optional[List[str]] = None) -> List[Dict]:
        """Batch parse multiple PDFs."""
        results = []
        for p in pdf_paths:
            try:
                res = self.parse(p)
                out_files = res.save(output_dir, formats)
                results.append({
                    "pdf": p,
                    "status": "success",
                    "project_name": res.project_name,
                    "report_type": res.report_type,
                    "total_rows": len(res.all_rows),
                    "grand_total": res.validation_summary.grand_total_computed,
                    "output_files": out_files,
                })
            except Exception as exc:
                logger.exception(f"Failed to parse {p}: {exc}")
                results.append({"pdf": p, "status": "error", "error": str(exc)})
        return results
