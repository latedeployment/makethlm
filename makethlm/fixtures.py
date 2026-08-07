"""Recorded LLM responses for deterministic, offline runs.

Recording a run stores each prompt/response pair under a stable key. Replaying
serves those responses instead of calling a provider, so a Promptfile can be
exercised in CI with no network access, no credentials, and no spend.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


class FixtureError(Exception):
    """Raised when a fixture cannot be read or written."""


def fixture_key(task_name: str, prompt: str) -> str:
    """Return the stable key identifying a prompt within a task."""
    digest = hashlib.sha256()
    digest.update(task_name.encode())
    digest.update(b"\0")
    digest.update(prompt.encode())
    return digest.hexdigest()[:32]


class FixtureStore:
    """Reads and writes prompt fixtures in a directory."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def path_for(self, task_name: str, prompt: str) -> Path:
        # The key is a hex digest, so it never escapes the directory.
        return self.directory / f"{fixture_key(task_name, prompt)}.json"

    def load(self, task_name: str, prompt: str) -> dict[str, object] | None:
        """Return the recorded fixture for a prompt, or None when absent."""
        path = self.path_for(task_name, prompt)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise FixtureError(f"cannot read fixture {path}: {e}")
        if not isinstance(data, dict) or "response" not in data:
            raise FixtureError(f"malformed fixture {path}")
        return data

    def save(
        self,
        task_name: str,
        prompt: str,
        response: str,
        *,
        success: bool,
        provider: str | None = None,
    ) -> Path:
        """Write a fixture atomically with owner-only permissions."""
        path = self.path_for(task_name, prompt)
        payload = {
            "task": task_name,
            "provider": provider,
            "prompt": prompt,
            "response": response,
            "success": success,
            "recorded_at": time.time(),
        }
        tmp_path: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.directory,
                prefix=".fixture-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True))
                tmp_path = Path(handle.name)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError as e:
            raise FixtureError(f"cannot write fixture {path}: {e}")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return path

    def count(self) -> int:
        """Return how many fixtures the directory currently holds."""
        if not self.directory.is_dir():
            return 0
        return len(list(self.directory.glob("*.json")))
