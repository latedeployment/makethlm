"""Git-aware inputs for Promptfile functions.

These let a task scope itself to what actually changed — reviewing a diff
against a base branch rather than the whole tree — without shelling out through
backticks.

All git invocations are fixed argv, read-only, and never run through a shell.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess

from .subprocess_util import run_subprocess

GIT_TIMEOUT_SECONDS = 30

# Environment variable set by ``--since`` and used as the default ref.
SINCE_ENV_VAR = "MAKETHLM_SINCE"


def default_ref() -> str:
    """Return the ref that git functions compare against by default."""
    return os.environ.get(SINCE_ENV_VAR) or "HEAD"


def _git(args: list[str], cwd: str | None = None) -> str | None:
    """Run a read-only git command and return its stdout, or None on failure."""
    try:
        result = run_subprocess(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def changed_files(ref: str | None = None, cwd: str | None = None) -> list[str]:
    """Return paths that differ from *ref*, including untracked files.

    Returns an empty list outside a repository or when git is unavailable, so a
    Promptfile stays usable in a plain directory.
    """
    target = ref or default_ref()
    paths: list[str] = []
    diff = _git(["diff", "--name-only", target], cwd=cwd)
    if diff is None:
        return []
    paths.extend(line.strip() for line in diff.splitlines() if line.strip())
    untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd=cwd)
    if untracked:
        for line in untracked.splitlines():
            path = line.strip()
            if path and path not in paths:
                paths.append(path)
    return paths


def matches(paths: list[str], pattern: str) -> bool:
    """Return whether any path matches a glob pattern.

    ``src/**`` matches anything under ``src/``; other patterns use standard
    shell-glob semantics against the whole path.
    """
    if pattern.endswith("/**"):
        prefix = pattern[:-2]
        return any(path.startswith(prefix) for path in paths)
    return any(fnmatch.fnmatch(path, pattern) for path in paths)


def branch(cwd: str | None = None) -> str:
    """Return the current branch name, or an empty string when unavailable."""
    output = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return output.strip() if output else ""


def sha(short: bool = True, cwd: str | None = None) -> str:
    """Return the current commit SHA, or an empty string when unavailable."""
    args = ["rev-parse", "--short", "HEAD"] if short else ["rev-parse", "HEAD"]
    output = _git(args, cwd=cwd)
    return output.strip() if output else ""
