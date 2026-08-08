"""Append-only log of every LLM call, for watching and debugging a run.

Run history records what a task produced; this records each individual call as
it happens — provider, timing, usage, and why the call was made (first attempt,
retry, contract repair, fan-out branch, judge). The file is JSONL so it can be
tailed during a run and filtered with standard tools afterwards.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Prompts and responses can be enormous; keep the log readable and bounded.
MAX_LOGGED_CHARS = 4000


@dataclass
class CallRecord:
    """One dispatch attempt."""

    task: str
    provider: str
    kind: str  # "prompt", "repair", "judge", or "fanout"
    attempt: int
    success: bool
    duration_ms: int
    prompt: str
    response: str
    source: str = "provider"  # "provider", "fixture", or "budget"
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z"))

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _truncate(text: str) -> str:
    if len(text) <= MAX_LOGGED_CHARS:
        return text
    return text[:MAX_LOGGED_CHARS] + f"\n[...truncated {len(text) - MAX_LOGGED_CHARS} chars]"


class CallLog:
    """Writes call records as JSONL, one line per dispatch attempt."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._failed = False

    def record(self, call: CallRecord) -> None:
        """Append one record. Logging never interrupts a run."""
        if self._failed:
            return
        payload = call.as_dict()
        payload["prompt"] = _truncate(str(payload["prompt"]))
        payload["response"] = _truncate(str(payload["response"]))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, sort_keys=True) + "\n")
            if not existed:
                os.chmod(self.path, 0o600)
        except OSError:
            # A broken log must not fail the task it was only observing.
            self._failed = True
