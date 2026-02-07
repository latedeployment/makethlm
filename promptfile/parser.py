"""Parser for the Promptfile format.

Uses a line-by-line scanner (no regex hot-path) for speed and clarity.

Syntax overview:

    # Comments
    project := "myapp"
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

    docker myapp [tag=latest]:
        Python 3.11 slim image.
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


_BOOLEAN_OPTIONS = {"private", "confirm"}


def _parse_kv_pairs(raw: str) -> dict[str, str]:
    """Parse 'key=value, key2=value2, bare_flag' into a dict.

    Bare keywords (no ``=``) are allowed for known boolean options like
    ``private`` and ``confirm``; they are stored as ``key -> "true"``.
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
        elif key == "working_dir" or key == "working-dir":
            opts.working_dir = value
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
    """Parse the inside of (arg1, arg2='default') into TaskArgument list."""
    raw = raw.strip()
    if not raw:
        return []
    args: list[TaskArgument] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, default = part.split("=", 1)
            name = name.strip()
            default = default.strip().strip('"').strip("'")
            args.append(TaskArgument(name=name, default=default))
        else:
            args.append(TaskArgument(name=part))
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
            while rest.startswith("@"):
                space_idx = rest.find(" ")
                if space_idx == -1:
                    break
                prefix = rest[:space_idx]
                if prefix == "@silent":
                    silent = True
                elif prefix == "@ignore":
                    ignore_error = True
                rest = rest[space_idx + 1 :].lstrip()
            steps.append(TaskStep(
                kind="shell",
                content=rest,
                silent=silent,
                ignore_error=ignore_error,
            ))
        else:
            prompt_accum.append(stripped)

    flush_prompt()
    return steps


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
            i += 1
            continue

        # ----- set <directive> [value] -----
        if stripped.startswith("set "):
            rest = stripped[4:].strip()
            if rest.startswith("dotenv-load"):
                val = rest[11:].strip()
                pf.settings.dotenv_load = val.lower() not in ("false", "no", "0") if val else True
            elif rest.startswith("shell"):
                val = rest[5:].strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                pf.settings.shell = val if val else None
            elif rest.startswith("working-dir") or rest.startswith("working_dir"):
                # consume "working-dir" or "working_dir"
                key_len = 11 if rest.startswith("working-dir") else 11
                val = rest[key_len:].strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                pf.settings.working_dir = val if val else None
            else:
                raise ParseError(f"unknown set directive: {rest!r}", lineno)
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

        # ----- variable := "value" or backtick variable := `command` -----
        if ":=" in stripped:
            eq_idx = stripped.index(":=")
            var_name = stripped[:eq_idx].strip()
            var_val_raw = stripped[eq_idx + 2 :].strip()
            # Backtick command substitution
            if var_val_raw.startswith("`") and var_val_raw.endswith("`"):
                cmd = var_val_raw[1:-1]
                try:
                    proc = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True, timeout=30,
                    )
                    pf.variables[var_name] = proc.stdout.strip()
                except (subprocess.TimeoutExpired, OSError) as e:
                    raise ParseError(f"backtick command failed: {e}", lineno)
                i += 1
                continue
            if not (var_val_raw.startswith('"') and var_val_raw.endswith('"')):
                raise ParseError(f"variable value must be quoted: {var_val_raw!r}", lineno)
            var_val = var_val_raw[1:-1]
            var_val = var_val.replace('\\"', '"').replace("\\\\", "\\")
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
            if task_name in pf.tasks:
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
