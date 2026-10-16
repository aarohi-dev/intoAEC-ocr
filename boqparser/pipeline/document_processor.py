"""
Document Processor — coordinates the end-to-end streaming extraction pipeline.

Pipeline stages:
1. Native page streaming via PDFProcessor (bounded memory, <200MB RAM).
2. Layout discovery & table reconstruction per chunk.
3. Domain correction (safe word-boundary vocabulary & unit normalization).
4. Hierarchy reconstruction (sections, bills, items).
5. Mathematical & schema validation.
6. Emits CanonicalDocument conforming to the output.json blueprint.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from boqparser.domain.domain_corrector import DomainCorrector
from boqparser.hierarchy.hierarchy_detector import HierarchyDetector, Section
from boqparser.models.intermediate import (
    CanonicalDocument,
    CanonicalItem,
    CanonicalSection,
    Provenance,
)
from boqparser.pdf.pdf_processor import PageData, PDFProcessor
from boqparser.pipeline.chunk_processor import ChunkProcessor
from boqparser.table.table_reconstructor import BOQRow, TableReconstructor
from boqparser.validation.validator import ValidationSummary, Validator

logger = logging.getLogger(__name__)


class DocumentProcessor:
    """End-to-end lightweight document extraction orchestrator."""

    def __init__(
        self,
        tolerance: float = 0.02,
        fuzzy_threshold: int = 85,
        ollama_adapter: Optional[Any] = None,
    ):
        self.pdf_processor = PDFProcessor()
        self.ollama_adapter = ollama_adapter
        self.chunk_processor = ChunkProcessor(ollama_adapter=ollama_adapter)
        self.domain_corrector = DomainCorrector(fuzzy_threshold=fuzzy_threshold)
        self.hierarchy_detector = HierarchyDetector()
        self.validator = Validator(tolerance=tolerance)

    def process_document(
        self,
        pdf_path: str,
        pages: Optional[List[int]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> CanonicalDocument:
        """
        Process a digital PDF document incrementally and return a CanonicalDocument.
        """
        path = str(Path(pdf_path).resolve())
        total_pages = self.pdf_processor.get_page_count(path)
        logger.info(f"Processing PDF: {path} ({total_pages} pages)")

        all_rows: List[BOQRow] = []

        # ── Step 1: Stream pages natively ────────────────────────────────────
        for idx, page in enumerate(self.pdf_processor.stream_pages(path, pages=pages), start=1):
            if progress_callback:
                progress_callback(idx, total_pages)

            chunk_rows = self.chunk_processor.process_page_chunk(page)
            for r in chunk_rows:
                # ── Step 2: Domain terminology & unit correction ─────────────
                if (
                    not getattr(r, "is_section_header", False)
                    and not getattr(r, "is_summary_row", False)
                    and not getattr(r, "is_subtotal_row", False)
                    and not getattr(r, "is_trade_heading", False)
                ):
                    self.domain_corrector.correct_row(r)
                all_rows.append(r)

        # ── Step 3: Reconstruct Hierarchy ────────────────────────────────────
        sections: List[Section] = self.hierarchy_detector.detect(all_rows)

        # ── Step 4: Mathematical Validation ──────────────────────────────────
        validation: ValidationSummary = self.validator.validate_all(all_rows, sections)

        # ── Step 5: Build CanonicalDocument matching blueprint ────────────────
        canonical_sections: List[CanonicalSection] = []
        for sec in sections:
            c_items: List[CanonicalItem] = []
            for it in sec.all_items():
                qty = getattr(it, "quantity", None)
                net_rate = getattr(it, "net_rate", None)
                if net_rate is None:
                    net_rate = getattr(it, "rate", None)
                gross_rate = getattr(it, "gross_rate", None)
                if gross_rate is None:
                    gross_rate = net_rate
                amount = getattr(it, "amount", None)

                c_items.append(
                    CanonicalItem(
                        item_no=str(getattr(it, "item_no", "") or ""),
                        description=str(getattr(it, "description", "") or "").strip(),
                        unit=str(getattr(it, "unit", "") or ""),
                        quantity=float(qty) if qty is not None else None,
                        net_rate=float(net_rate) if net_rate is not None else None,
                        gross_rate=float(gross_rate) if gross_rate is not None else None,
                        amount=float(amount) if amount is not None else None,
                        materials=getattr(it, "materials", []) or [],
                        provenance=Provenance(page_num=getattr(it, "page_num", 1)),
                    )
                )

            canonical_sections.append(
                CanonicalSection(
                    section_code=sec.section_code or "0001",
                    section_title=sec.section_title or "General",
                    items=c_items,
                    subtotal=sec.subtotal,
                )
            )

        doc = CanonicalDocument(
            source_file=path,
            sections=canonical_sections,
            total_pages=total_pages,
            metadata={
                "tree_sections": sections,
                "total_rows": len(all_rows),
                "sections_found": len(canonical_sections),
                "valid_rows": validation.valid_rows,
                "invalid_rows": validation.invalid_rows,
                "grand_total": validation.grand_total_declared or validation.grand_total_computed,
                "grand_total_computed": validation.grand_total_computed,
                "grand_total_declared": validation.grand_total_declared,
                "reconciliation": validation.reconciliation,
                "reconciliation_valid": validation.reconciliation_valid,
            },
            validation_issues=[
                {
                    "item_no": v.row.item_no,
                    "description": v.row.description,
                    "issue": v.flag_message,
                    "issue_type": v.issue_type,
                }
                for v in validation.row_validations
                if not v.is_valid
            ],
        )

        logger.info(
            f"Extraction complete: {len(canonical_sections)} sections, "
            f"{len(doc.all_items())} items extracted from {total_pages} pages."
        )

        return doc
