"""Template interpolation and environment expansion helpers."""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable


def split_unquoted(text: str, separator: str) -> list[str]:
    """Split on a one-character separator outside quotes and nested groups."""
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    depth = 0
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote:
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
            continue
        if char in "({[":
            depth += 1
        elif char in ")}]" and depth:
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def resolve_env_vars(text: str) -> str:
    """Resolve ``${VAR:-default}`` and ``${VAR}`` from the environment."""

    def _with_default(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(2))

    text = re.sub(r"\$\{(\w+):-([^}]*)\}", _with_default, text)

    def _braced(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), "")

    return re.sub(r"\$\{(\w+)\}", _braced, text)


def apply_parameter_expansion(var_value: str, operator: str, pattern: str) -> str:
    """Apply bash-style parameter expansion to a value."""
    if operator == "#":
        for i in range(len(var_value) + 1):
            if fnmatch.fnmatch(var_value[:i], pattern):
                return var_value[i:]

    elif operator == "##":
        for i in range(len(var_value), -1, -1):
            if fnmatch.fnmatch(var_value[:i], pattern):
                return var_value[i:]

    elif operator == "%":
        for i in range(len(var_value), -1, -1):
            if fnmatch.fnmatch(var_value[i:], pattern):
                return var_value[:i]

    elif operator == "%%":
        for i in range(len(var_value) + 1):
            if fnmatch.fnmatch(var_value[i:], pattern):
                return var_value[:i]

    elif operator == "//":
        parts = pattern.split("/", 1)
        old = parts[0]
        new = parts[1] if len(parts) > 1 else ""
        return var_value.replace(old, new)

    elif operator == "/":
        parts = pattern.split("/", 1)
        old = parts[0]
        new = parts[1] if len(parts) > 1 else ""
        return var_value.replace(old, new, 1)

    return var_value


def try_parameter_expansion(expr: str, context: dict[str, str]) -> str | None:
    """Try to parse bash-style parameter expansion from an expression."""
    for op in ("##", "#"):
        idx = expr.find(op)
        if idx > 0:
            var_name = expr[:idx].strip()
            pattern = expr[idx + len(op) :]
            if var_name in context:
                return apply_parameter_expansion(context[var_name], op, pattern)

    for op in ("%%", "%"):
        idx = expr.find(op)
        if idx > 0:
            var_name = expr[:idx].strip()
            pattern = expr[idx + len(op) :]
            if var_name in context:
                return apply_parameter_expansion(context[var_name], op, pattern)

    for op in ("//", "/"):
        idx = expr.find(op)
        if idx > 0:
            var_name = expr[:idx].strip()
            pattern = expr[idx + len(op) :]
            if var_name in context:
                return apply_parameter_expansion(context[var_name], op, pattern)

    return None


def interpolate_text(
    text: str,
    context: dict[str, str],
    *,
    string_functions: dict[str, Callable[..., str]],
    parse_function_args: Callable[[str], list[str]],
    call_function: Callable[[str, list[str], dict[str, str]], str | None],
    evaluate_expression: Callable[[str, dict[str, str]], str],
) -> str:
    """Replace ``{{name}}`` and evaluate supported template expressions."""

    def _replace_match(m: re.Match[str]) -> str:
        inner = m.group(1).strip()

        if inner in context:
            return context[inner]

        paren_pos = inner.find("(")
        if paren_pos != -1 and inner.endswith(")"):
            fn_name = inner[:paren_pos].strip()
            if fn_name in string_functions:
                args_raw = inner[paren_pos + 1 : -1]
                args = parse_function_args(args_raw)
                try:
                    result = call_function(fn_name, args, context)
                    if result is not None:
                        return result
                except (ValueError, TypeError):
                    return m.group(0)

        expansion_result = try_parameter_expansion(inner, context)
        if expansion_result is not None:
            return expansion_result

        if inner.startswith("if ") or "+" in inner or inner.endswith("()"):
            return evaluate_expression(inner, context)

        return m.group(0)

    return re.sub(r"\{\{(.+?)\}\}", _replace_match, text)
