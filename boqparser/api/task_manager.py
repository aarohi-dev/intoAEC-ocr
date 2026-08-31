"""
Task Manager & Page-level Cache Database Persistence.
Manages background task states and caches page-level OCR results in SQLite.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = Path("boq_tasks_and_cache.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT,
    progress INTEGER,
    pdf_path TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS page_cache (
    pdf_hash TEXT,
    page_num INTEGER,
    ocr_result TEXT,
    created_at TEXT,
    PRIMARY KEY (pdf_hash, page_num)
);
"""

class TaskManager:
    """Manages SQLite database for background tasks and incremental OCR page caching."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.executescript(_SCHEMA)
                conn.commit()
        except Exception as exc:
            logger.error(f"Failed to initialize TaskManager DB: {exc}")

    @staticmethod
    def get_pdf_hash(pdf_path: str) -> str:
        """
        Generate a stable hash for a PDF based purely on its size and content.

        IMPORTANT: the filename is intentionally excluded.  Uploads arrive as
        temp files with random names (e.g. tmp3a8f1b.pdf), so including the
        name would produce a different hash for every upload of the same file,
        breaking the page-level OCR cache entirely.
        """
        p = Path(pdf_path)
        if not p.exists():
            return hashlib.sha256(pdf_path.encode()).hexdigest()

        hasher = hashlib.sha256()
        # Size makes the hash cheaper: a different-size file can't collide.
        hasher.update(str(p.stat().st_size).encode())

        try:
            with open(p, "rb") as f:
                hasher.update(f.read(8192))          # first 8 KB
                if p.stat().st_size > 16384:
                    f.seek(-8192, 2)
                    hasher.update(f.read(8192))      # last 8 KB
        except Exception:
            pass

        return hasher.hexdigest()

    # ── Task management ──────────────────────────────────────────────────────

    def create_task(self, task_id: str, pdf_path: str):
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "INSERT INTO tasks (task_id, status, progress, pdf_path, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, "processing", 0, pdf_path, datetime.now().isoformat())
                )
                conn.commit()
        except Exception as exc:
            logger.error(f"TaskManager: failed to create task {task_id}: {exc}")

    def update_task_progress(self, task_id: str, progress: int, status: str = "processing"):
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "UPDATE tasks SET progress = ?, status = ? WHERE task_id = ?",
                    (progress, status, task_id)
                )
                conn.commit()
        except Exception as exc:
            logger.error(f"TaskManager: failed to update task progress {task_id}: {exc}")

    def complete_task(self, task_id: str, result_dict: dict):
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "UPDATE tasks SET status = ?, progress = ?, result = ? WHERE task_id = ?",
                    ("completed", 100, json.dumps(result_dict), task_id)
                )
                conn.commit()
        except Exception as exc:
            logger.error(f"TaskManager: failed to complete task {task_id}: {exc}")

    def fail_task(self, task_id: str, error_msg: str):
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "UPDATE tasks SET status = ?, error = ? WHERE task_id = ?",
                    ("failed", error_msg, task_id)
                )
                conn.commit()
        except Exception as exc:
            logger.error(f"TaskManager: failed to fail task {task_id}: {exc}")

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
                row = cur.fetchone()
                if row:
                    task = dict(row)
                    if task.get("result"):
                        task["result"] = json.loads(task["result"])
                    return task
        except Exception as exc:
            logger.error(f"TaskManager: failed to get task {task_id}: {exc}")
        return None

    # ── Page/Chunk-level OCR Cache ───────────────────────────────────────────

    def get_cached_page(self, pdf_hash: str, page_num: int) -> Optional[Dict[str, Any]]:
        """Retrieve cached OCR result for a page if it exists."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cur = conn.execute(
                    "SELECT ocr_result FROM page_cache WHERE pdf_hash = ? AND page_num = ?",
                    (pdf_hash, page_num)
                )
                row = cur.fetchone()
                if row:
                    return json.loads(row[0])
        except Exception as exc:
            logger.error(f"TaskManager: failed to get cached page {page_num} for hash {pdf_hash}: {exc}")
        return None

    def cache_page(self, pdf_hash: str, page_num: int, ocr_result_dict: dict):
        """Save parsed page result to SQLite page cache."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO page_cache (pdf_hash, page_num, ocr_result, created_at) VALUES (?, ?, ?, ?)",
                    (pdf_hash, page_num, json.dumps(ocr_result_dict), datetime.now().isoformat())
                )
                conn.commit()
        except Exception as exc:
            logger.error(f"TaskManager: failed to cache page {page_num} for hash {pdf_hash}: {exc}")
