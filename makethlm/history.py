"""SQLite-backed run history for local/self-hosted use."""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .runner import RunResult
from .secrets import redact_text, secret_values_from_mapping


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
    parent_existed = db_path.parent.exists()
    db_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not parent_existed:
        os.chmod(db_path.parent, 0o700)
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
        # Older databases predate cost accounting; add the columns in place.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        for column, ddl in (
            ("tokens_in", "INTEGER NOT NULL DEFAULT 0"),
            ("tokens_out", "INTEGER NOT NULL DEFAULT 0"),
            ("cost_usd", "REAL NOT NULL DEFAULT 0"),
            ("llm_calls", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {ddl}")
    os.chmod(db_path, 0o600)
    return db_path


def record_run(
    result: RunResult,
    *,
    duration_ms: int,
    promptfile_path: str | None,
    path: Path | None = None,
    redact: Callable[[str], str] | None = None,
    costs: dict[str, Any] | None = None,
) -> int:
    """Store a run result and return the inserted run id."""
    db_path = init_history(path)
    if redact is None:
        secret_values = secret_values_from_mapping(dict(os.environ))

        def redact(value: str) -> str:
            return redact_text(value, secret_values)

    tasks: list[dict[str, Any]] = [
        {
            "task": tr.task_name,
            "success": tr.success,
            "prompt": redact(tr.prompt_sent),
            "response": redact(tr.response),
            "steps": [
                {
                    "kind": sr.kind,
                    "content": redact(sr.content),
                    "response": redact(sr.response),
                    "success": sr.success,
                    "host": sr.host,
                    "exit_code": sr.exit_code,
                    "provider": sr.provider,
                    "attempt": sr.attempt,
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
                started_at, target, success, duration_ms, promptfile, task_count,
                tasks_json, tokens_in, tokens_out, cost_usd, llm_calls
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                result.target,
                1 if result.success else 0,
                duration_ms,
                promptfile_path,
                len(result.task_results),
                json.dumps(tasks),
                int((costs or {}).get("tokens_in", 0)),
                int((costs or {}).get("tokens_out", 0)),
                float((costs or {}).get("cost_usd", 0.0)),
                int((costs or {}).get("calls", 0)),
            ),
        )
        return int(cur.lastrowid or 0)


def list_runs(limit: int = 20, *, path: Path | None = None) -> list[dict[str, Any]]:
    """Return recent runs from newest to oldest."""
    db_path = init_history(path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, started_at, target, success, duration_ms, promptfile, task_count,
                   tasks_json, tokens_in, tokens_out, cost_usd, llm_calls
            FROM runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_run(run_id: int, *, path: Path | None = None) -> dict[str, Any] | None:
    """Return one replayable run bundle, or ``None`` when it does not exist."""
    db_path = init_history(path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, started_at, target, success, duration_ms, promptfile,
                   task_count, tasks_json, tokens_in, tokens_out, cost_usd, llm_calls
            FROM runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        return None
    bundle = dict(row)
    bundle["success"] = bool(bundle["success"])
    try:
        bundle["tasks"] = json.loads(bundle.pop("tasks_json"))
    except (TypeError, json.JSONDecodeError):
        bundle["tasks"] = []
    bundle["schema"] = 1
    return bundle
