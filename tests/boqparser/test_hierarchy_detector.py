"""Tests for HierarchyDetector."""

from __future__ import annotations

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from boqparser.hierarchy.hierarchy_detector import HierarchyDetector, Section, _parse_depth, _ITEM_NO_RE
from boqparser.table.table_reconstructor import BOQRow


def make_section_row(item_no: str, title: str, page_num: int = 1) -> BOQRow:
    return BOQRow(item_no=item_no, description=title, quantity=None, rate=None, amount=None, page_num=page_num)


def make_item_row(item_no: str, desc: str, qty: float, unit: str, rate: float, amount: float) -> BOQRow:
    return BOQRow(item_no=item_no, description=desc, quantity=qty, unit=unit, rate=rate, amount=amount)


class TestItemNumberParsing:
    def test_depth_0(self):
        assert _parse_depth("1") == 0
        assert _parse_depth("2") == 0

    def test_depth_1(self):
        assert _parse_depth("1.1") == 1
        assert _parse_depth("2.3") == 1

    def test_depth_2(self):
        assert _parse_depth("1.1.1") == 2

    def test_depth_3(self):
        assert _parse_depth("1.1.1.1") == 3

    def test_invalid(self):
        assert _parse_depth("ABC") == -1
        assert _parse_depth("") == -1

    def test_regex(self):
        assert _ITEM_NO_RE.match("1")
        assert _ITEM_NO_RE.match("1.1")
        assert _ITEM_NO_RE.match("1.1.1")
        assert _ITEM_NO_RE.match("1A")
        assert not _ITEM_NO_RE.match("abc")
        assert not _ITEM_NO_RE.match("FOUNDATION")


class TestHierarchyDetector:
    def test_simple_two_level(self):
        rows = [
            make_section_row("1", "FOUNDATION WORKS"),
            make_section_row("1.1", "Earth Work"),
            make_item_row("1.1.1", "Excavation", 100, "Cft", 20, 2000),
            make_item_row("1.1.2", "Earth filling", 50, "Cft", 10, 500),
            make_section_row("1.2", "PCC Work"),
            make_item_row("1.2.1", "PCC 1:4:8", 20, "CuM", 4500, 90000),
        ]
        hd = HierarchyDetector()
        sections = hd.detect(rows)

        assert len(sections) == 1
        assert sections[0].number == "1"
        assert sections[0].title == "FOUNDATION WORKS"
        assert len(sections[0].children) == 2
        assert sections[0].children[0].number == "1.1"
        assert sections[0].children[1].number == "1.2"

    def test_items_attached_to_correct_section(self):
        rows = [
            make_section_row("1.1", "Earth Work"),
            make_item_row("1.1.1", "Excavation", 100, "Cft", 20, 2000),
            make_item_row("1.1.2", "Earth filling", 50, "Cft", 10, 500),
        ]
        hd = HierarchyDetector()
        sections = hd.detect(rows)

        assert len(sections) == 1
        s = sections[0]
        assert len(s.items) == 2
        assert s.items[0].item_no == "1.1.1"
        assert s.items[1].item_no == "1.1.2"

    def test_multiple_top_level_sections(self):
        rows = [
            make_section_row("1", "FOUNDATION"),
            make_item_row("1.1", "Footing", 10, "Cft", 50, 500),
            make_section_row("2", "SUPERSTRUCTURE"),
            make_item_row("2.1", "Columns", 20, "Cft", 100, 2000),
        ]
        hd = HierarchyDetector()
        sections = hd.detect(rows)
        assert len(sections) == 2
        assert sections[0].number == "1"
        assert sections[1].number == "2"

    def test_orphan_items(self):
        rows = [
            make_item_row("", "Miscellaneous item", 5, "No.", 100, 500),
        ]
        hd = HierarchyDetector()
        sections = hd.detect(rows)
        assert len(sections) >= 1
        # Item should be attached to a default section
        all_items = sections[0].all_items()
        assert len(all_items) == 1

    def test_flatten(self):
        rows = [
            make_section_row("1", "FOUNDATION"),
            make_item_row("1.1", "Footing", 10, "Cft", 50, 500),
            make_item_row("1.2", "Foundation slab", 5, "CuM", 4500, 22500),
        ]
        hd = HierarchyDetector()
        sections = hd.detect(rows)
        flat = hd.flatten(sections)
        assert len(flat) == 2

    def test_get_all_sections(self):
        rows = [
            make_section_row("1", "FOUNDATION"),
            make_section_row("1.1", "Earth Work"),
            make_item_row("1.1.1", "Excavation", 100, "Cft", 20, 2000),
        ]
        hd = HierarchyDetector()
        sections = hd.detect(rows)
        all_secs = hd.get_all_sections(sections)
        numbers = [s.number for s in all_secs]
        assert "1" in numbers
        assert "1.1" in numbers

    def test_section_to_dict(self):
        sec = Section(number="1.1", title="Foundation Works", depth=1)
        d = sec.to_dict()
        assert d["number"] == "1.1"
        assert d["title"] == "Foundation Works"
        assert "items" in d
        assert "children" in d
