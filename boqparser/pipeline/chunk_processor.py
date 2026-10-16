"""
Chunk Processor — bounded-context processing for document page batches.

Processes pages incrementally within memory constraints:
- Extracts native text and geometry for a single page chunk.
- Invokes OllamaAdapter only if column headers are ambiguous.
- Emits structured BOQRow records.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from boqparser.layout.structure_discovery import StructureDiscovery, TableStructure
from boqparser.mapping.ollama_adapter import OllamaAdapter
from boqparser.mapping.semantic_mapper import SemanticMapper
from boqparser.pdf.pdf_processor import PageData, PDFProcessor
from boqparser.table.table_reconstructor import BOQRow, TableReconstructor

logger = logging.getLogger(__name__)


class ChunkProcessor:
    """Processes bounded page chunks to maintain low memory footprint."""

    def __init__(
        self,
        ollama_adapter: Optional[OllamaAdapter] = None,
        table_reconstructor: Optional[TableReconstructor] = None,
    ):
        self.ollama_adapter = ollama_adapter or OllamaAdapter()
        self.table_reconstructor = table_reconstructor or TableReconstructor(ollama_adapter=self.ollama_adapter)
        self.active_structure: Optional[TableStructure] = None

    def process_page_chunk(
        self,
        page: PageData,
    ) -> List[BOQRow]:
        """
        Process a single page chunk natively without full-document overhead.
        """
        rows, new_struct = self.table_reconstructor.reconstruct_page(
            page,
            active_structure=self.active_structure,
        )
        if new_struct:
            self.active_structure = new_struct

        return rows
