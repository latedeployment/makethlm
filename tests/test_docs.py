"""Documented Promptfile snippets must actually parse.

The README's opening example is the first thing a reader copies, so a syntax
error there is worse than a broken test. This walks every fenced block in the
README and docs and parses the ones that are meant to be real Promptfiles.

Only *syntax* errors fail the test. Snippets that reference a task, function, or
host group defined elsewhere on the page are legitimate fragments.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from makethlm.parser import ParseError, parse

REPO = Path(__file__).resolve().parent.parent

# Errors that mean the snippet itself is malformed, rather than incomplete.
SYNTAX_ERRORS = (
    "must end with",
    "unexpected line",
    "must be quoted",
    "must be an integer",
    "missing name",
    "invalid option",
    "unknown option",
    "expected",
)

# Reference blocks are schematic rather than runnable: grammar placeholders like
# `<name>` and `[tag=...]`, or a cheat-sheet whose every line carries an aligned
# trailing `# label`. Detected by content so the list cannot go stale.
_PLACEHOLDER = re.compile(r"<[a-z_]+>|=\s*\.\.\.")
_ANNOTATED = re.compile(r"^\S.*\s{2,}#\s\S", re.M)


def _is_schematic(body: str) -> bool:
    return bool(_PLACEHOLDER.search(body) or _ANNOTATED.search(body))


# Lines that mark a block as prose or a shell transcript rather than a Promptfile.
_NOT_PROMPTFILE = re.compile(r"^\s*(\$|#!|makethlm\s)")
_DECLARATION = re.compile(r"^\s*(task|fn|hosts|docker|mcp|llm|agent)\s", re.M)


def _fenced_blocks(text: str) -> list[tuple[str, str]]:
    """Return (info_string, body) for every fenced block, pairing fences in order."""
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("```"):
            index += 1
            continue
        info = line[3:].strip()
        body: list[str] = []
        index += 1
        while index < len(lines) and not lines[index].startswith("```"):
            body.append(lines[index])
            index += 1
        index += 1  # step past the closing fence
        blocks.append((info, "\n".join(body)))
    return blocks


def _promptfile_snippets() -> list[tuple[str, int, str]]:
    """Collect the fenced blocks that are meant to be Promptfile source."""
    found: list[tuple[str, int, str]] = []
    paths = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]
    for path in paths:
        rel = str(path.relative_to(REPO))
        for position, (info, body) in enumerate(_fenced_blocks(path.read_text())):
            if info in ("bash", "sh", "shell", "console", "json", "text", "toml", "yaml"):
                continue
            if not body.strip() or not _DECLARATION.search(body):
                continue
            if _NOT_PROMPTFILE.match(body.lstrip("\n")):
                continue
            if _is_schematic(body):
                continue
            found.append((rel, position, body))
    return found


SNIPPETS = _promptfile_snippets()


def test_snippets_were_found():
    # A silent zero would make the whole file a no-op.
    assert len(SNIPPETS) > 20


@pytest.mark.parametrize(
    "rel,position,body",
    SNIPPETS,
    ids=[f"{rel}#{position}" for rel, position, _ in SNIPPETS],
)
def test_documented_snippet_has_no_syntax_errors(rel, position, body):
    try:
        parse(body)
    except ParseError as e:
        if any(marker in str(e) for marker in SYNTAX_ERRORS):
            pytest.fail(f"{rel} block #{position} has a syntax error: {e}\n\n{body}")
    except Exception:
        # Snippets that reach for the filesystem or a shell are fragments.
        pass
