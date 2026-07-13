"""SQLite DB Exporter — exports BOQ data to a relational SQLite database."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, List


_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    report_type     TEXT,
    source_file     TEXT,
    total_pages     INTEGER,
    pdf_type        TEXT,
    ocr_mode        TEXT,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id),
    parent_id       INTEGER REFERENCES sections(id),
    number          TEXT,
    title           TEXT,
    depth           INTEGER,
    subtotal        REAL,
    page_num        INTEGER
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id),
    section_id      INTEGER REFERENCES sections(id),
    item_no         TEXT,
    description     TEXT,
    quantity        REAL,
    unit            TEXT,
    rate            REAL,
    amount          REAL,
    page_num        INTEGER,
    is_subtotal_row INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS validation_issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER REFERENCES projects(id),
    item_id         INTEGER REFERENCES items(id),
    issue_type      TEXT,
    expected_amount REAL,
    actual_amount   REAL,
    error_pct       REAL,
    flag_message    TEXT
);
"""


class DBExporter:
    """Export BOQ parse result to a SQLite database."""

    def export(self, parse_result: Any, output_path: str) -> str:
        """Write to SQLite file. Returns absolute path."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(out))
        try:
            conn.executescript(_SCHEMA)

            # Insert project
            cur = conn.execute(
                """INSERT INTO projects
                   (name, report_type, source_file, total_pages, pdf_type, ocr_mode, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    getattr(parse_result, "project_name", ""),
                    getattr(parse_result, "report_type", ""),
                    getattr(parse_result, "source_file", ""),
                    getattr(parse_result, "total_pages", 0),
                    getattr(parse_result, "pdf_type", ""),
                    getattr(parse_result, "ocr_mode", ""),
                    datetime.now().isoformat(),
                )
            )
            project_id = cur.lastrowid

            # Insert sections and items recursively
            validation_pairs: List[tuple] = []  # (item_id, ValidationResult)

            vs = getattr(parse_result, "validation_summary", None)
            # Build a lookup from item (item_no, page_num) → ValidationResult
            vr_lookup = {}
            if vs:
                for vr in vs.row_validations:
                    key = (vr.row.item_no, vr.row.page_num)
                    vr_lookup[key] = vr

            for sec in getattr(parse_result, "sections", []):
                self._insert_section(conn, project_id, sec, parent_id=None, vr_lookup=vr_lookup)

            conn.commit()
        finally:
            conn.close()

        return str(out.resolve())

    def _insert_section(self, conn, project_id, section, parent_id, vr_lookup):
        cur = conn.execute(
            """INSERT INTO sections (project_id, parent_id, number, title, depth, subtotal, page_num)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (project_id, parent_id, section.number, section.title,
             section.depth, section.subtotal, section.page_num)
        )
        section_id = cur.lastrowid

        for item in section.items:
            if item.is_header_row:
                continue
            item_cur = conn.execute(
                """INSERT INTO items
                   (project_id, section_id, item_no, description, quantity, unit, rate, amount, page_num, is_subtotal_row)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (project_id, section_id, item.item_no, item.description,
                 item.quantity, item.unit, item.rate, item.amount, item.page_num,
                 1 if item.is_subtotal_row else 0)
            )
            item_id = item_cur.lastrowid

            # Insert validation issue if present
            key = (item.item_no, item.page_num)
            vr = vr_lookup.get(key)
            if vr and not vr.is_valid:
                conn.execute(
                    """INSERT INTO validation_issues
                       (project_id, item_id, issue_type, expected_amount, actual_amount, error_pct, flag_message)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (project_id, item_id, vr.issue_type,
                     vr.expected_amount, vr.actual_amount, vr.error_pct, vr.flag_message)
                )

        for child in section.children:
            self._insert_section(conn, project_id, child, section_id, vr_lookup)
