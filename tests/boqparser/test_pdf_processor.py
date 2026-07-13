"""Tests for PDFProcessor."""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from boqparser.pdf.pdf_processor import PDFProcessor, PageImage

SAMPLE_PDF = os.path.join(os.path.dirname(__file__), "..", "gnarly_pdfs", "edgar.pdf")
SMALL_PDF  = os.path.join(os.path.dirname(__file__), "..", "gnarly_pdfs", "small_page_size.pdf")


@pytest.mark.skipif(not os.path.exists(SAMPLE_PDF), reason="Sample PDF not found")
class TestPDFProcessor:
    def test_page_count(self):
        proc = PDFProcessor()
        count = proc.get_page_count(SAMPLE_PDF)
        assert count >= 1

    def test_render_page(self):
        proc = PDFProcessor(dpi=150)  # Low DPI for speed in tests
        img = proc.render_page(SAMPLE_PDF, page_num=1)
        assert isinstance(img, PageImage)
        assert img.page_num == 1
        assert img.width_px > 0
        assert img.height_px > 0
        assert img.dpi == 300  # minimum enforced

    def test_dpi_minimum_enforced(self):
        proc = PDFProcessor(dpi=72)  # below minimum
        assert proc.dpi == 300

    def test_get_page_dimensions(self):
        proc = PDFProcessor()
        dims = proc.get_page_dimensions(SAMPLE_PDF)
        assert len(dims) >= 1
        for d in dims:
            assert d["width_pts"] > 0
            assert d["height_pts"] > 0

    def test_detect_pdf_type_digital(self):
        proc = PDFProcessor()
        pdf_type = proc.detect_pdf_type(SAMPLE_PDF)
        assert pdf_type in ("digital", "scanned", "mixed")

    def test_nonexistent_pdf(self):
        proc = PDFProcessor()
        with pytest.raises(FileNotFoundError):
            proc.convert_to_images("/nonexistent/path.pdf")

    def test_convert_to_images(self):
        proc = PDFProcessor(dpi=150)
        pages = proc.convert_to_images(SAMPLE_PDF, pages=[1])
        assert len(pages) == 1
        assert isinstance(pages[0].image, object)  # PIL Image
