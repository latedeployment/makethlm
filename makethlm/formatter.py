"""Canonical formatting for Promptfiles.

The formatter only touches layout: indentation, spacing inside option
brackets, trailing whitespace, and blank lines between top-level blocks. Prompt
prose and shell command text are never rewritten, and relative indentation
inside a body is preserved so script recipes keep their structure.
"""

from __future__ import annotations

INDENT = "    "

# Keywords that begin a top-level block.
_BLOCK_KEYWORDS = ("task ", "fn ", "hosts ", "docker ", "agent ", "mod ")

# Top-level single-line declarations.
_DECLARATION_KEYWORDS = ("set ", "llm ", "alias ", "include ", "import ", "export ")


def _is_block_header(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith(_BLOCK_KEYWORDS):
        return True
    # A bare Just-style recipe header: "name:" or "name: deps" at column 0.
    if line[:1] not in (" ", "\t", "#", "") and stripped.endswith(":"):
        return True
    return False


def _is_declaration(line: str) -> bool:
    return line[:1] not in (" ", "\t") and line.strip().startswith(_DECLARATION_KEYWORDS)


def format_option_brackets(line: str) -> str:
    """Normalize spacing inside a trailing ``[...]`` option list."""
    start = line.find("[")
    end = line.rfind("]")
    if start == -1 or end == -1 or end < start:
        return line
    inner = line[start + 1 : end]
    if not inner.strip():
        return line
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            continue
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    items.append("".join(current).strip())
    normalized = ", ".join(item for item in items if item)
    return f"{line[:start]}[{normalized}]{line[end + 1 :]}"


def _common_indent(lines: list[str]) -> str:
    """Return the whitespace prefix shared by every non-blank line."""
    prefixes = [line[: len(line) - len(line.lstrip())] for line in lines if line.strip()]
    if not prefixes:
        return ""
    shortest = min(prefixes, key=len)
    for prefix in prefixes:
        if not prefix.startswith(shortest):
            # Mixed tabs and spaces: leave the body alone.
            return ""
    return shortest


def format_text(text: str) -> str:
    """Return the canonically formatted Promptfile source.

    Blank lines the author wrote between blocks are preserved (collapsed to at
    most one) rather than inserted, because grouping related declarations is a
    deliberate choice in a file that is partly prose.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            # Collapse runs of blank lines; drop them at the start of the file.
            if out:
                out.append("")
            while index < len(lines) and not lines[index].strip():
                index += 1
            continue

        if _is_block_header(line):
            out.append(format_option_brackets(line))
            index += 1
            body: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip() and candidate[:1] not in (" ", "\t"):
                    break
                body.append(candidate)
                index += 1
            had_trailing_blank = bool(body) and not body[-1].strip()
            while body and not body[-1].strip():
                body.pop()
            out.extend(_reindent(body))
            if had_trailing_blank:
                # Keep the author's separation between this block and the next.
                out.append("")
            continue

        out.append(line)
        index += 1

    while out and not out[-1].strip():
        out.pop()
    if not out:
        return ""
    return "\n".join(out) + "\n"


def _reindent(body: list[str]) -> list[str]:
    """Re-indent a block body to the canonical indent, keeping its structure."""
    indent = _common_indent(body)
    rebuilt: list[str] = []
    for line in body:
        if not line.strip():
            rebuilt.append("")
        elif indent and line.startswith(indent):
            rebuilt.append(INDENT + line[len(indent) :])
        else:
            rebuilt.append(INDENT + line.lstrip())
    return rebuilt


def needs_formatting(text: str) -> bool:
    """Return whether the source differs from its formatted form."""
    return format_text(text) != text
