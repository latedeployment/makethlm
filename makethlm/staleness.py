"""File-based staleness checks for make-style task skipping.

Tasks declaring ``sources`` and ``outputs`` are skipped when every output
already exists and is at least as new as every matched source. Sources also
contribute a content digest to task cache keys so time-based caches expire as
soon as their inputs change on disk.
"""

from __future__ import annotations

import glob
import hashlib
import os
from pathlib import Path

# Guard against a pattern such as "**/*" in a large tree consuming the run.
MAX_MATCHED_FILES = 5000

# Only read this many bytes per file when digesting sources.
MAX_DIGEST_BYTES = 8 * 1024 * 1024


def split_patterns(value: str) -> list[str]:
    """Split a ``sources``/``outputs`` option value into patterns.

    Commas and pipes separate patterns; whitespace does not, so paths
    containing spaces stay intact.
    """
    parts: list[str] = []
    for chunk in value.replace("|", ",").split(","):
        pattern = chunk.strip()
        if pattern:
            parts.append(pattern)
    return parts


def _is_glob(pattern: str) -> bool:
    """Return True when the pattern contains wildcard syntax."""
    return any(char in pattern for char in "*?[")


def expand_patterns(patterns: list[str], base_dir: str | None = None) -> list[Path]:
    """Return the sorted, de-duplicated files matching ``patterns``.

    Directories are ignored; only regular files participate in staleness.
    Relative patterns resolve against ``base_dir`` (default: current
    directory).
    """
    root = Path(base_dir) if base_dir else Path.cwd()
    matched: set[Path] = set()
    for pattern in patterns:
        candidate = pattern if os.path.isabs(pattern) else str(root / pattern)
        for hit in glob.glob(candidate, recursive=True):
            path = Path(hit)
            if path.is_file():
                matched.add(path)
            if len(matched) > MAX_MATCHED_FILES:
                raise ValueError(f"pattern {pattern!r} matched more than {MAX_MATCHED_FILES} files")
    return sorted(matched)


def digest_sources(patterns: list[str], base_dir: str | None = None) -> str | None:
    """Return a content digest for the files matched by ``patterns``.

    Returns ``None`` when no patterns are configured so callers can leave the
    cache key untouched. Unreadable files are recorded by name so a permission
    change still invalidates the key.
    """
    if not patterns:
        return None
    try:
        paths = expand_patterns(patterns, base_dir)
    except ValueError:
        # Too many matches to digest cheaply; treat as always-changed.
        return "unbounded"
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path).encode())
        digest.update(b"\0")
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read(MAX_DIGEST_BYTES))
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def up_to_date_reason(
    sources: list[str],
    outputs: list[str],
    base_dir: str | None = None,
) -> str | None:
    """Return a human-readable reason when a task can be skipped.

    Returns ``None`` when the task must run. A task is up to date only when
    both patterns are configured, at least one source matched, every output
    matched at least one existing file, and the oldest output is at least as
    new as the newest source.
    """
    if not sources or not outputs:
        return None
    try:
        source_paths = expand_patterns(sources, base_dir)
        output_paths = expand_patterns(outputs, base_dir)
    except ValueError:
        return None
    if not source_paths:
        # A typo or a not-yet-created tree: run rather than skip.
        return None
    root = Path(base_dir) if base_dir else Path.cwd()
    for pattern in outputs:
        # A literal (non-glob) output that does not exist means stale.
        if not _is_glob(pattern):
            candidate = Path(pattern) if os.path.isabs(pattern) else root / pattern
            if not candidate.exists():
                return None
    if not output_paths:
        return None
    try:
        newest_source = max(path.stat().st_mtime for path in source_paths)
        oldest_output = min(path.stat().st_mtime for path in output_paths)
    except OSError:
        return None
    if oldest_output < newest_source:
        return None
    count = len(source_paths)
    noun = "source" if count == 1 else "sources"
    return f"up to date ({count} {noun} older than outputs)"
