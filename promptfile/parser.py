"""Parser for the Promptfile format.

Uses a line-by-line scanner (no regex hot-path) for speed and clarity.

Syntax overview (Justfile-compatible + LLM extensions):

    # Comments
    set dotenv-load
    set shell "/bin/bash"
    set export
    set positional-arguments
    set fallback
    set quiet

    project := "myapp"
    version := `git describe --tags`
    export API_KEY := "secret"
    full_name := "prefix" + project + "-suffix"

    include "common.pf"

    llm claude [model=opus]
    llm openai [model=gpt-4, key=$OPENAI_API_KEY]

    hosts web [user=deploy, port=22]:
        web1.example.com
        web2.example.com

    fn security_review:
        Review code for security vulnerabilities.

    task build:
        !mkdir -p dist
        compile the project

    task deploy(target, port="8080"): build [llm=openai, on=web]:
        deploy {{project}} to {{target}} on port {{port}}

    task linux-only [os=linux, private, no-exit-message, confirm]:
        linux specific stuff

    task greet(+names):
        say hello to {{names}}

    docker myapp [tag=latest]:
        Python 3.11 slim image.

    alias d := deploy
"""

from __future__ import annotations

import os
import subprocess

from .models import (
    DockerConfig,
    Function,
    HostGroup,
    LLMProvider,
    Promptfile,
    Settings,
    Task,
    TaskArgument,
    TaskOptions,
    TaskStep,
    _evaluate_expression,
)


class ParseError(Exception):
    """Raised when the Promptfile contains invalid syntax."""

    def __init__(self, message: str, line_number: int | None = None):
        self.line_number = line_number
        prefix = f"line {line_number}: " if line_number else ""
        super().__init__(f"{prefix}{message}")


# ---------------------------------------------------------------------------
# Low-level helpers (no regex)
# ---------------------------------------------------------------------------

def _strip_trailing_colon(s: str) -> tuple[str, bool]:
    """If s ends with ':', return (s[:-1].rstrip(), True), else (s, False)."""
    stripped = s.rstrip()
    if stripped.endswith(":"):
        return stripped[:-1].rstrip(), True
    return stripped, False


def _extract_brackets(s: str) -> tuple[str, str | None]:
    """Extract [...] from a string. Returns (remaining, bracket_content | None)."""
    start = s.find("[")
    if start == -1:
        return s, None
    end = s.find("]", start)
    if end == -1:
        return s, None
    bracket_content = s[start + 1 : end]
    remaining = s[:start] + s[end + 1 :]
    return remaining.strip(), bracket_content


_BOOLEAN_OPTIONS = {
    "private", "confirm", "no-cd", "no_cd",
    "no-exit-message", "no_exit_message",
    "no-quiet", "no_quiet",
    "positional-arguments", "positional_arguments",
    # OS-specific attributes (Justfile-compatible)
    "linux", "macos", "windows", "unix",
}


def _parse_kv_pairs(raw: str) -> dict[str, str]:
    """Parse 'key=value, key2=value2, bare_flag' into a dict.

    Bare keywords (no ``=``) are allowed for known boolean options;
    they are stored as ``key -> "true"``.
    """
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            if pair in _BOOLEAN_OPTIONS:
                result[pair] = "true"
            else:
                raise ParseError(f"invalid option (expected key=value): {pair!r}")
        else:
            key, value = pair.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _parse_task_options(kvs: dict[str, str]) -> TaskOptions:
    """Convert a key-value dict into TaskOptions."""
    opts = TaskOptions()
    for key, value in kvs.items():
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
        elif key == "llm":
            opts.llm = value
        elif key == "on":
            opts.on = value
        elif key == "private":
            opts.private = value.lower() in ("true", "yes", "1")
        elif key == "group":
            opts.group = value
        elif key == "doc":
            opts.doc = value
        elif key == "confirm":
            if value.lower() in ("true", "yes", "1"):
                opts.confirm = True
            else:
                opts.confirm = value
        elif key == "os":
            opts.os_filter = value
        elif key in ("working_dir", "working-dir"):
            opts.working_dir = value
        elif key in ("no-cd", "no_cd"):
            opts.no_cd = value.lower() in ("true", "yes", "1")
        elif key in ("no-exit-message", "no_exit_message"):
            opts.no_exit_message = value.lower() in ("true", "yes", "1")
        elif key in ("no-quiet", "no_quiet"):
            opts.no_quiet = value.lower() in ("true", "yes", "1")
        elif key in ("positional-arguments", "positional_arguments"):
            opts.positional_arguments = value.lower() in ("true", "yes", "1")
        # Bare OS attributes (Justfile-compatible: [linux], [macos], etc.)
        elif key in ("linux", "macos", "windows", "unix"):
            opts.os_filter = key
        else:
            raise ParseError(f"unknown option: {key!r}")
    return opts


def _parse_docker_config(kvs: dict[str, str]) -> DockerConfig:
    """Convert a key-value dict into DockerConfig."""
    cfg = DockerConfig()
    for key, value in kvs.items():
        if key == "tag":
            cfg.tag = value
        elif key == "context":
            cfg.context = value
        elif key == "file":
            cfg.file = value
        else:
            raise ParseError(f"unknown docker option: {key!r}")
    return cfg


def _parse_arguments(raw: str) -> list[TaskArgument]:
    """Parse the inside of (arg1, arg2='default', +variadic) into TaskArgument list."""
    raw = raw.strip()
    if not raw:
        return []
    args: list[TaskArgument] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        # Variadic args: +names (one or more), *names (zero or more)
        variadic = None
        if part.startswith("+") or part.startswith("*"):
            variadic = part[0]
            part = part[1:]
        if "=" in part:
            name, default = part.split("=", 1)
            name = name.strip()
            default = default.strip().strip('"').strip("'")
            args.append(TaskArgument(name=name, default=default, variadic=variadic))
        else:
            args.append(TaskArgument(name=part.strip(), variadic=variadic))
    return args


# ---------------------------------------------------------------------------
# Body collection
# ---------------------------------------------------------------------------

def _is_blank(line: str) -> bool:
    return not line or line.isspace()


def _is_indented(line: str) -> bool:
    return len(line) > 0 and line[0] in (" ", "\t")


def _collect_body(lines: list[str], start: int) -> tuple[list[str], int]:
    """Collect indented body lines from `start`. Returns (body_lines, next_index)."""
    n = len(lines)
    i = start
    body: list[str] = []

    while i < n:
        line = lines[i]
        if _is_blank(line):
            # Include blank lines only if more indented lines follow
            j = i + 1
            while j < n and _is_blank(lines[j]):
                j += 1
            if j < n and _is_indented(lines[j]):
                body.append("")
                i += 1
                continue
            else:
                break
        elif _is_indented(line):
            body.append(line)
            i += 1
        else:
            break

    return body, i


def _parse_body_steps(raw_lines: list[str]) -> list[TaskStep]:
    """Parse indented body lines into TaskSteps.

    Lines starting with ! become shell steps.
    Consecutive non-shell lines merge into a single prompt step.
    """
    steps: list[TaskStep] = []
    prompt_accum: list[str] = []

    def flush_prompt() -> None:
        if prompt_accum:
            text = "\n".join(prompt_accum).strip()
            if text:
                steps.append(TaskStep(kind="prompt", content=text))
            prompt_accum.clear()

    for raw in raw_lines:
        stripped = raw.strip()
        if stripped.startswith("!"):
            flush_prompt()
            rest = stripped[1:]
            # Parse @-prefixes
            silent = False
            ignore_error = False
            quiet = False
            # Check for bare @ prefix (quiet — suppress echoing)
            if rest.startswith("@") and not rest.startswith("@silent") and not rest.startswith("@ignore"):
                quiet = True
                rest = rest[1:]
            while rest.startswith("@"):
                space_idx = rest.find(" ")
                if space_idx == -1:
                    break
                prefix = rest[:space_idx]
                if prefix == "@silent":
                    silent = True
                elif prefix == "@ignore":
                    ignore_error = True
                elif prefix == "@quiet":
                    quiet = True
                rest = rest[space_idx + 1 :].lstrip()
            steps.append(TaskStep(
                kind="shell",
                content=rest,
                silent=silent,
                ignore_error=ignore_error,
                quiet=quiet,
            ))
        else:
            prompt_accum.append(stripped)

    flush_prompt()
    return steps


def _strip_quotes(val: str) -> str:
    """Strip surrounding single or double quotes."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def _parse_set_directive(rest: str, pf: Promptfile, lineno: int) -> None:
    """Parse a 'set <directive> [value]' line."""
    # Map directive names to settings fields
    directives = {
        "dotenv-load": "dotenv_load",
        "dotenv-path": "dotenv_path",
        "dotenv-required": "dotenv_required",
        "shell": "shell",
        "working-dir": "working_dir",
        "working_dir": "working_dir",
        "export": "export",
        "positional-arguments": "positional_arguments",
        "positional_arguments": "positional_arguments",
        "fallback": "fallback",
        "ignore-comments": "ignore_comments",
        "ignore_comments": "ignore_comments",
        "tempdir": "tempdir",
        "quiet": "quiet",
        "allow-duplicate-tasks": "allow_duplicate_tasks",
        "allow-duplicate-variables": "allow_duplicate_variables",
    }

    # Find which directive matches
    matched_key = None
    matched_field = None
    for key, field_name in directives.items():
        if rest.startswith(key):
            # Make sure it's a complete word match
            after = rest[len(key):]
            if not after or after[0] in (" ", "\t"):
                matched_key = key
                matched_field = field_name
                break

    if matched_key is None:
        raise ParseError(f"unknown set directive: {rest!r}", lineno)

    val = rest[len(matched_key):].strip()
    val = _strip_quotes(val)

    # Boolean directives
    bool_fields = {
        "dotenv_load", "dotenv_required", "export",
        "positional_arguments", "fallback", "ignore_comments",
        "quiet", "allow_duplicate_tasks", "allow_duplicate_variables",
    }
    if matched_field in bool_fields:
        bool_val = val.lower() not in ("false", "no", "0") if val else True
        setattr(pf.settings, matched_field, bool_val)
    else:
        # String directives
        setattr(pf.settings, matched_field, val if val else None)


# ---------------------------------------------------------------------------
# Main parser — line scanner, no regex on hot path
# ---------------------------------------------------------------------------

def parse(
    source: str,
    filename: str = "Promptfile",
    *,
    _included: set[str] | None = None,
    _base_dir: str | None = None,
) -> Promptfile:
    """Parse a Promptfile source string into a Promptfile model."""
    pf = Promptfile()

    # Handle line continuation (\) before splitting
    source = _join_continuations(source)

    lines = source.split("\n")
    i = 0
    n = len(lines)

    if _included is None:
        _included = set()
    abs_filename = os.path.abspath(filename)
    _included.add(abs_filename)

    if _base_dir is None:
        _base_dir = os.path.dirname(abs_filename) or "."

    while i < n:
        line = lines[i]
        lineno = i + 1
        stripped = line.strip()

        # Skip blanks and comments
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        # ----- include "path" -----
        if stripped.startswith("include "):
            rest = stripped[8:].strip()
            if not (rest.startswith('"') and rest.endswith('"')):
                raise ParseError(f"include path must be quoted: {rest!r}", lineno)
            include_path = rest[1:-1]
            resolved = os.path.normpath(os.path.join(_base_dir, include_path))
            if resolved in _included:
                raise ParseError(f"circular include: {include_path!r}", lineno)
            try:
                with open(resolved) as f:
                    include_source = f.read()
            except FileNotFoundError:
                raise ParseError(f"included file not found: {include_path!r}", lineno)
            included_pf = parse(
                include_source, resolved,
                _included=_included, _base_dir=os.path.dirname(resolved),
            )
            # Merge (included defs can be overridden by local)
            for k, v in included_pf.variables.items():
                pf.variables.setdefault(k, v)
            for k, v in included_pf.functions.items():
                pf.functions.setdefault(k, v)
            for name in included_pf.task_order:
                if name not in pf.tasks:
                    pf.tasks[name] = included_pf.tasks[name]
                    pf.task_order.append(name)
            for k, v in included_pf.llm_providers.items():
                pf.llm_providers.setdefault(k, v)
            if included_pf.default_llm and not pf.default_llm:
                pf.default_llm = included_pf.default_llm
            for k, v in included_pf.host_groups.items():
                pf.host_groups.setdefault(k, v)
            pf.exported_vars.update(included_pf.exported_vars)
            for k, v in included_pf.aliases.items():
                pf.aliases.setdefault(k, v)
            i += 1
            continue

        # ----- set <directive> [value] -----
        if stripped.startswith("set "):
            _parse_set_directive(stripped[4:].strip(), pf, lineno)
            i += 1
            continue

        # ----- alias <name> := <target> -----
        if stripped.startswith("alias "):
            rest = stripped[6:].strip()
            if ":=" not in rest:
                raise ParseError("alias must use ':=' syntax, e.g. alias d := deploy", lineno)
            eq_idx = rest.index(":=")
            alias_name = rest[:eq_idx].strip()
            alias_target = rest[eq_idx + 2 :].strip()
            if not alias_name:
                raise ParseError("alias missing name", lineno)
            if not alias_target:
                raise ParseError("alias missing target", lineno)
            pf.aliases[alias_name] = alias_target
            i += 1
            continue

        # ----- export VAR := "value" -----
        if stripped.startswith("export "):
            rest = stripped[7:].strip()
            if ":=" in rest:
                eq_idx = rest.index(":=")
                var_name = rest[:eq_idx].strip()
                var_val_raw = rest[eq_idx + 2 :].strip()
                var_val = _parse_var_value(var_val_raw, pf, lineno)
                pf.variables[var_name] = var_val
                pf.exported_vars.add(var_name)
                i += 1
                continue
            # Bare "export VAR" — mark existing var as exported
            var_name = rest.strip()
            if var_name:
                pf.exported_vars.add(var_name)
                i += 1
                continue
            raise ParseError("export requires a variable name", lineno)

        # ----- variable := "value" / `cmd` / expression -----
        if ":=" in stripped:
            eq_idx = stripped.index(":=")
            var_name = stripped[:eq_idx].strip()
            var_val_raw = stripped[eq_idx + 2 :].strip()
            var_val = _parse_var_value(var_val_raw, pf, lineno)
            pf.variables[var_name] = var_val
            i += 1
            continue

        # ----- llm <name> [opts] -----
        if stripped.startswith("llm "):
            rest = stripped[4:].strip()
            rest, bracket = _extract_brackets(rest)
            llm_name = rest.strip()
            if not llm_name:
                raise ParseError("llm declaration missing name", lineno)
            llm_opts = _parse_kv_pairs(bracket) if bracket else {}
            api_key = llm_opts.get("key")
            if api_key and api_key.startswith("$"):
                api_key = os.environ.get(api_key[1:], api_key)
            provider = LLMProvider(
                name=llm_name,
                model=llm_opts.get("model"),
                api_key=api_key,
                base_url=llm_opts.get("base_url"),
                shell_template=llm_opts.get("template"),
            )
            pf.llm_providers[llm_name] = provider
            if pf.default_llm is None:
                pf.default_llm = llm_name
            i += 1
            continue

        # ----- hosts <name> [opts]: -----
        if stripped.startswith("hosts "):
            rest, has_colon = _strip_trailing_colon(stripped[6:])
            if not has_colon:
                raise ParseError("hosts block must end with ':'", lineno)
            rest, bracket = _extract_brackets(rest)
            group_name = rest.strip()
            if not group_name:
                raise ParseError("hosts declaration missing name", lineno)
            host_kvs = _parse_kv_pairs(bracket) if bracket else {}
            for k in host_kvs:
                if k not in ("user", "port"):
                    raise ParseError(f"unknown host option: {k!r}", lineno)
            if group_name in pf.host_groups:
                raise ParseError(f"duplicate host group: {group_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            host_list = [l.strip() for l in body_lines if l.strip()]
            if not host_list:
                raise ParseError(f"host group {group_name!r} has no hosts", lineno)

            port = None
            if "port" in host_kvs:
                try:
                    port = int(host_kvs["port"])
                except ValueError:
                    raise ParseError(f"port must be an integer", lineno)

            pf.host_groups[group_name] = HostGroup(
                name=group_name,
                hosts=host_list,
                user=host_kvs.get("user"),
                port=port,
                line_number=lineno,
            )
            continue

        # ----- fn <name>: -----
        if stripped.startswith("fn "):
            rest, has_colon = _strip_trailing_colon(stripped[3:])
            if not has_colon:
                raise ParseError("fn block must end with ':'", lineno)
            fn_name = rest.strip()
            if not fn_name:
                raise ParseError("fn declaration missing name", lineno)
            if fn_name in pf.functions:
                raise ParseError(f"duplicate function: {fn_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            body = "\n".join(l.strip() for l in body_lines).strip()
            if not body:
                raise ParseError(f"function {fn_name!r} has no body", lineno)
            pf.functions[fn_name] = Function(name=fn_name, body=body, line_number=lineno)
            continue

        # ----- docker <name> [opts]: -----
        if stripped.startswith("docker "):
            rest, has_colon = _strip_trailing_colon(stripped[7:])
            if not has_colon:
                raise ParseError("docker block must end with ':'", lineno)
            rest, bracket = _extract_brackets(rest)
            docker_name = rest.strip()
            if not docker_name:
                raise ParseError("docker declaration missing name", lineno)
            docker_cfg = _parse_docker_config(_parse_kv_pairs(bracket)) if bracket else DockerConfig()

            if docker_name in pf.tasks:
                raise ParseError(f"duplicate task/docker: {docker_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            description = "\n".join(l.strip() for l in body_lines).strip()
            if not description:
                raise ParseError(f"docker {docker_name!r} has no description", lineno)

            task = Task(
                name=docker_name,
                steps=[TaskStep(kind="prompt", content=description)],
                docker=docker_cfg,
                line_number=lineno,
            )
            pf.tasks[docker_name] = task
            pf.task_order.append(docker_name)
            continue

        # ----- task <name>[(args)] [: deps] [opts]: -----
        if stripped.startswith("task "):
            rest, has_colon = _strip_trailing_colon(stripped[5:])
            if not has_colon:
                raise ParseError("task header must end with ':'", lineno)

            # Extract arguments (...)
            arguments: list[TaskArgument] = []
            paren_start = rest.find("(")
            if paren_start != -1:
                paren_end = rest.find(")", paren_start)
                if paren_end == -1:
                    raise ParseError("unclosed parenthesis in task header", lineno)
                arguments = _parse_arguments(rest[paren_start + 1 : paren_end])
                rest = rest[:paren_start] + rest[paren_end + 1 :]

            # Extract [options]
            rest, bracket = _extract_brackets(rest)
            options = _parse_task_options(_parse_kv_pairs(bracket)) if bracket else TaskOptions()

            # What remains: "name" or "name: dep1 dep2"
            rest = rest.strip()
            deps: list[str] = []
            if ":" in rest:
                colon_idx = rest.index(":")
                task_name = rest[:colon_idx].strip()
                dep_str = rest[colon_idx + 1 :].strip()
                if dep_str:
                    deps = dep_str.split()
            else:
                task_name = rest.strip()

            if not task_name:
                raise ParseError("task declaration missing name", lineno)

            # Tasks starting with _ are implicitly private (Justfile convention)
            if task_name.startswith("_"):
                options.private = True

            if task_name in pf.tasks:
                if not pf.settings.allow_duplicate_tasks:
                    raise ParseError(f"duplicate task: {task_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            steps = _parse_body_steps(body_lines)
            if not steps:
                raise ParseError(f"task {task_name!r} has no prompt body", lineno)

            task = Task(
                name=task_name,
                steps=steps,
                dependencies=deps,
                options=options,
                arguments=arguments,
                line_number=lineno,
            )
            pf.tasks[task_name] = task
            if task_name not in pf.task_order:
                pf.task_order.append(task_name)
            continue

        raise ParseError(f"unexpected line: {line!r}", lineno)

    # --- Validation ---
    for task in pf.tasks.values():
        for dep in task.dependencies:
            if dep not in pf.tasks:
                raise ParseError(
                    f"task {task.name!r} depends on unknown task {dep!r}",
                    task.line_number,
                )
        # Validate @use references
        for step in task.steps:
            if step.kind == "prompt":
                for text_line in step.content.split("\n"):
                    text_line = text_line.strip()
                    if text_line.startswith("@use "):
                        fn_name = text_line[5:].strip()
                        if fn_name not in pf.functions:
                            raise ParseError(
                                f"task {task.name!r} references unknown function {fn_name!r}",
                                task.line_number,
                            )
        # Validate [on=group] references
        if task.options.on and task.options.on not in pf.host_groups:
            raise ParseError(
                f"task {task.name!r} targets unknown host group {task.options.on!r}",
                task.line_number,
            )

    # Validate alias targets
    for alias_name, alias_target in pf.aliases.items():
        if alias_target not in pf.tasks:
            raise ParseError(f"alias {alias_name!r} targets unknown task {alias_target!r}")

    return pf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _join_continuations(source: str) -> str:
    """Join lines ending with \\ (line continuation, like Makefile/Justfile)."""
    result: list[str] = []
    lines = source.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        while line.endswith("\\") and i + 1 < len(lines):
            line = line[:-1] + lines[i + 1].lstrip()
            i += 1
        result.append(line)
        i += 1
    return "\n".join(result)


def _parse_var_value(raw: str, pf: Promptfile, lineno: int) -> str:
    """Parse a variable value: quoted string, backtick command, or expression."""
    raw = raw.strip()

    # Backtick command substitution
    if raw.startswith("`") and raw.endswith("`"):
        cmd = raw[1:-1]
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30,
            )
            return proc.stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as e:
            raise ParseError(f"backtick command failed: {e}", lineno)

    # String concatenation with +
    if "+" in raw:
        parts = raw.split("+")
        result_parts: list[str] = []
        for part in parts:
            part = part.strip()
            if (part.startswith('"') and part.endswith('"')) or \
               (part.startswith("'") and part.endswith("'")):
                result_parts.append(part[1:-1])
            elif part in pf.variables:
                result_parts.append(pf.variables[part])
            else:
                result_parts.append(part)
        return "".join(result_parts)

    # if/else expression
    if raw.startswith("if "):
        return _evaluate_expression(raw, pf.variables)

    # Quoted string
    if raw.startswith('"') and raw.endswith('"'):
        val = raw[1:-1]
        return val.replace('\\"', '"').replace("\\\\", "\\")

    # Single-quoted string
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]

    raise ParseError(f"variable value must be quoted, backtick, or expression: {raw!r}", lineno)
