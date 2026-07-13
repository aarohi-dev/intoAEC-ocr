"""Tests for TableReconstructor."""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from boqparser.table.table_reconstructor import (
    TableReconstructor,
    BOQRow,
    _parse_html_table,
    _to_float,
    _is_number,
    _is_unit,
    _is_item_no,
    _is_header_row,
    _positional_column_map,
    _cells_to_row,
)


class TestHelpers:
    def test_to_float(self):
        assert _to_float("1234.56") == 1234.56
        assert _to_float("1,234.56") == 1234.56
        assert _to_float("abc") is None
        assert _to_float("") is None
        assert _to_float(None) is None

    def test_is_number(self):
        assert _is_number("100")
        assert _is_number("1,234.56")
        assert _is_number("-50.5")
        assert not _is_number("RCC")
        assert not _is_number("")

    def test_is_unit(self):
        assert _is_unit("cft")
        assert _is_unit("Cft")
        assert _is_unit("sqft")
        assert _is_unit("nos.")
        assert not _is_unit("RCC")
        assert not _is_unit("Foundation")

    def test_is_item_no(self):
        assert _is_item_no("1")
        assert _is_item_no("1.1")
        assert _is_item_no("1.1.1")
        assert _is_item_no("1.1.1.1")
        assert _is_item_no("1A")
        assert not _is_item_no("RCC")
        assert not _is_item_no("")

    def test_is_header_row(self):
        assert _is_header_row(["Item", "Description", "Qty", "Unit", "Rate", "Amount"])
        assert _is_header_row(["Sl.No", "Particulars", "Quantity", "Unit"])
        assert not _is_header_row(["1.1", "Foundation Works", "100", "Cft", "25", "2500"])

    def test_positional_column_map_6cols(self):
        m = _positional_column_map(6)
        assert m[0] == "item_no"
        assert m[1] == "description"
        assert m[2] == "quantity"
        assert m[3] == "unit"
        assert m[4] == "rate"
        assert m[5] == "amount"

    def test_cells_to_row(self):
        col_map = {0: "item_no", 1: "description", 2: "quantity", 3: "unit", 4: "rate", 5: "amount"}
        cells = ["1.1.1", "Earth Work for Footing", "6035.750", "Cft", "11.00", "66393.25"]
        row = _cells_to_row(cells, col_map, page_num=1)
        assert row.item_no == "1.1.1"
        assert row.description == "Earth Work for Footing"
        assert row.quantity == 6035.750
        assert row.unit == "Cft"
        assert row.rate == 11.00
        assert row.amount == 66393.25


class TestHTMLTableParser:
    def test_standard_html_table(self):
        html = """
        <table>
          <tr><th>Item No</th><th>Description</th><th>Qty</th><th>Unit</th><th>Rate</th><th>Amount</th></tr>
          <tr><td>1.1.1</td><td>Earth Work for Footing</td><td>6035.750</td><td>Cft</td><td>11.00</td><td>66393.25</td></tr>
          <tr><td>1.1.2</td><td>PCC 1:4:8</td><td>150.5</td><td>CuM</td><td>4500</td><td>677250</td></tr>
        </table>
        """
        rows = _parse_html_table(html, page_num=1)
        assert len(rows) >= 2
        r = rows[0]
        assert r.item_no == "1.1.1"
        assert "Earth Work" in r.description
        assert r.quantity == 6035.75
        assert r.unit == "Cft"
        assert r.rate == 11.00
        assert r.amount == 66393.25

    def test_empty_html(self):
        rows = _parse_html_table("", page_num=1)
        assert rows == []

    def test_no_header_html(self):
        html = """
        <table>
          <tr><td>1</td><td>Foundation</td><td>100</td><td>Cft</td><td>50</td><td>5000</td></tr>
        </table>
        """
        rows = _parse_html_table(html, page_num=1)
        assert len(rows) >= 1


class TestMarkdownTable:
    def test_parse_markdown_table(self):
        md = """
| Item No | Description | Qty | Unit | Rate | Amount |
|---------|-------------|-----|------|------|--------|
| 1.1 | Foundation Works | 100 | Cft | 50 | 5000 |
| 1.2 | PCC | 20 | CuM | 4500 | 90000 |
"""
        rec = TableReconstructor()
        rows = rec.reconstruct(
            tokens=[],
            raw_html=None,
            raw_text=md,
            is_table=True,
            page_num=1,
        )
        assert len(rows) >= 2

    def test_reconstruct_with_raw_text(self):
        raw = "1.1.1  Earth Work for Footing  6035.750  Cft  11.00  66393.25"
        from boqparser.adapter.ocr_adapter import OCRToken
        tokens = [OCRToken(text=raw, confidence=0.9, bbox=[0, 0, 0, 0], page_num=1)]
        rec = TableReconstructor()
        rows = rec.reconstruct(tokens=tokens, raw_text=raw, page_num=1)
        assert len(rows) >= 1


class TestBOQRow:
    def test_to_dict(self):
        row = BOQRow(item_no="1.1", description="Foundation", quantity=100.0, unit="Cft", rate=50.0, amount=5000.0)
        d = row.to_dict()
        assert d["item_no"] == "1.1"
        assert d["description"] == "Foundation"
        assert d["quantity"] == 100.0
        assert d["unit"] == "Cft"
        assert d["rate"] == 50.0
        assert d["amount"] == 5000.0
