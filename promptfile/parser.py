"""Parser for the Promptfile format.

Syntax overview:

    # Comments
    project := "myapp"

    task build:
        check if moo.md is newer, if so build docker from scratch

    task deploy: build
        deploy {{project}} to production

    task review [model=opus, temperature=0.2]:
        review the git diff for security issues
"""

from __future__ import annotations

import re

from .models import Promptfile, Task, TaskOptions


class ParseError(Exception):
    """Raised when the Promptfile contains invalid syntax."""

    def __init__(self, message: str, line_number: int | None = None):
        self.line_number = line_number
        prefix = f"line {line_number}: " if line_number else ""
        super().__init__(f"{prefix}{message}")


# Patterns
_COMMENT_RE = re.compile(r"^\s*#")
_BLANK_RE = re.compile(r"^\s*$")
_VARIABLE_RE = re.compile(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*:=\s*"((?:[^"\\]|\\.)*)"\s*$')
_TASK_RE = re.compile(
    r"^task\s+"
    r"([a-zA-Z_][a-zA-Z0-9_-]*)"  # task name
    r"(?:\s*:\s*([a-zA-Z_][a-zA-Z0-9_\- ]*))?"  # optional dependencies after ':'
    r"(?:\s*\[([^\]]*)\])?"  # optional [options]
    r"\s*:\s*$"  # trailing colon
)
# Alternate: task with deps uses first colon for deps, but we need to
# disambiguate "task name:" from "task name: dep1 dep2:".
# Revised approach: the deps come after the name separated by colon,
# options in brackets, then a final colon ends the header.
#
# Actually let's simplify. Two forms:
#   task NAME:                          (no deps)
#   task NAME: dep1 dep2:              (with deps — note trailing colon)
#   task NAME [opts]:                   (no deps, with opts)
#   task NAME: dep1 dep2 [opts]:       (deps + opts)
#
# The trailing colon is always required and marks end-of-header.
# When deps are present, the first colon separates name from deps.
#
# Regex strategy: match greedily, then parse the interior.

_TASK_HEADER_RE = re.compile(
    r"^task\s+([a-zA-Z_][a-zA-Z0-9_-]*)"  # 'task' keyword + name
    r"(.*)"  # everything else
    r":\s*$"  # must end with ':'
)

_OPTIONS_RE = re.compile(r"\[([^\]]*)\]")


def _parse_options(raw: str) -> TaskOptions:
    """Parse a bracket-delimited options string like 'model=opus, temperature=0.2'."""
    opts = TaskOptions()
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ParseError(f"invalid option (expected key=value): {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key == "model":
            opts.model = value
        elif key == "temperature":
            try:
                opts.temperature = float(value)
            except ValueError:
                raise ParseError(f"temperature must be a number, got {value!r}")
        elif key == "max_tokens":
            try:
                opts.max_tokens = int(value)
            except ValueError:
                raise ParseError(f"max_tokens must be an integer, got {value!r}")
        else:
            raise ParseError(f"unknown option: {key!r}")
    return opts


def parse(source: str, filename: str = "Promptfile") -> Promptfile:
    """Parse a Promptfile source string into a Promptfile model."""
    pf = Promptfile()
    lines = source.split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        lineno = i + 1  # 1-indexed for error messages

        # Skip blanks and comments
        if _BLANK_RE.match(line) or _COMMENT_RE.match(line):
            i += 1
            continue

        # Try variable assignment
        m = _VARIABLE_RE.match(line)
        if m:
            name, value = m.group(1), m.group(2)
            # Unescape
            value = value.replace('\\"', '"').replace("\\\\", "\\")
            pf.variables[name] = value
            i += 1
            continue

        # Try task header
        m = _TASK_HEADER_RE.match(line)
        if m:
            task_name = m.group(1)
            rest = m.group(2).strip()
            # rest is everything between task name and the final ':'
            # It may contain ': dep1 dep2 [opts]' or '[opts]' or ': dep1 dep2' or ''

            deps: list[str] = []
            options = TaskOptions()

            # Extract options if present
            opt_match = _OPTIONS_RE.search(rest)
            if opt_match:
                options = _parse_options(opt_match.group(1))
                rest = rest[: opt_match.start()] + rest[opt_match.end() :]
                rest = rest.strip()

            # What remains could be ': dep1 dep2' or empty
            if rest.startswith(":"):
                dep_str = rest[1:].strip()
                if dep_str:
                    deps = dep_str.split()
            elif rest:
                raise ParseError(
                    f"unexpected content in task header: {rest!r}", lineno
                )

            if task_name in pf.tasks:
                raise ParseError(f"duplicate task: {task_name!r}", lineno)

            # Collect indented prompt lines
            i += 1
            prompt_lines: list[str] = []
            while i < n:
                pline = lines[i]
                # Prompt lines must be indented (start with whitespace)
                # and we stop at blank-then-non-indented or non-indented
                if _BLANK_RE.match(pline):
                    # Blank line — include it if more indented lines follow
                    # Peek ahead
                    j = i + 1
                    while j < n and _BLANK_RE.match(lines[j]):
                        j += 1
                    if j < n and lines[j].startswith((" ", "\t")):
                        prompt_lines.append("")
                        i += 1
                        continue
                    else:
                        break
                elif pline.startswith((" ", "\t")):
                    prompt_lines.append(pline.strip())
                    i += 1
                else:
                    break

            prompt = "\n".join(prompt_lines).strip()
            if not prompt:
                raise ParseError(
                    f"task {task_name!r} has no prompt body", lineno
                )

            task = Task(
                name=task_name,
                prompt=prompt,
                dependencies=deps,
                options=options,
                line_number=lineno,
            )
            pf.tasks[task_name] = task
            pf.task_order.append(task_name)
            continue

        raise ParseError(f"unexpected line: {line!r}", lineno)

    # Validate dependencies exist
    for task in pf.tasks.values():
        for dep in task.dependencies:
            if dep not in pf.tasks:
                raise ParseError(
                    f"task {task.name!r} depends on unknown task {dep!r}",
                    task.line_number,
                )

    return pf
