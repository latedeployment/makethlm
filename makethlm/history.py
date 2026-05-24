"""SQLite-backed run history for local/self-hosted use."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import RunResult


def history_db_path() -> Path:
    """Return the history database path, honoring MAKETHLM_HISTORY_DB."""
    override = os.environ.get("MAKETHLM_HISTORY_DB")
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "makethlm" / "history.sqlite"


def init_history(path: Path | None = None) -> Path:
    """Create the history database if needed and return its path."""
    db_path = path or history_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                target TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                promptfile TEXT,
                task_count INTEGER NOT NULL,
                tasks_json TEXT NOT NULL
            )
            """
        )
    return db_path


def record_run(
    result: RunResult,
    *,
    duration_ms: int,
    promptfile_path: str | None,
    path: Path | None = None,
) -> int:
    """Store a run result and return the inserted run id."""
    db_path = init_history(path)
    tasks: list[dict[str, Any]] = [
        {
            "task": tr.task_name,
            "success": tr.success,
            "steps": [
                {
                    "kind": sr.kind,
                    "success": sr.success,
                    "host": sr.host,
                }
                for sr in tr.step_results
            ],
        }
        for tr in result.task_results
    ]
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (
                started_at, target, success, duration_ms, promptfile, task_count, tasks_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                result.target,
                1 if result.success else 0,
                duration_ms,
                promptfile_path,
                len(result.task_results),
                json.dumps(tasks),
            ),
        )
        return int(cur.lastrowid)


def list_runs(limit: int = 20, *, path: Path | None = None) -> list[dict[str, Any]]:
    """Return recent runs from newest to oldest."""
    db_path = init_history(path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, started_at, target, success, duration_ms, promptfile, task_count, tasks_json
            FROM runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
