"""
BOQ Parser - Production-grade PDF parsing system for construction documents.

Wraps the existing olmocr VLM OCR engine and adds:
- PDF processing (pypdfium2)
- Layout analysis
- Table reconstruction
- Hierarchy detection
- Domain correction (construction vocabulary)
- Validation (quantity × rate ≈ amount)
- Multi-format export (JSON, CSV, XLSX, Markdown, SQLite)
- FastAPI service
- CLI interface
"""

from boqparser.parser import BOQParser, ParseResult
from boqparser.adapter.ocr_adapter import OCRAdapter
from boqparser.pdf.pdf_processor import PDFProcessor

__all__ = ["BOQParser", "ParseResult", "OCRAdapter", "PDFProcessor"]
__version__ = "1.0.0"
