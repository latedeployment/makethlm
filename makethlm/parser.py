"""Parser for the Promptfile format.

Uses a line-by-line scanner (no regex hot-path) for speed and clarity.

Syntax overview (Justfile-compatible + LLM extensions):

    # Comments
    set dotenv-load                 # load .env
    set dotenv-load ".env.local"    # load specific file
    set shell "/bin/bash"
    set export
    set positional-arguments
    set quiet

    project := "myapp"
    version := `git describe --tags`
    export API_KEY := "secret"
    full_name := "prefix" + project + "-suffix"

    include "common.pf"

    llm claude [model=opus]
    llm openai [model=gpt-4, key=$OPENAI_API_KEY]

    mcp files [command="npx -y server-filesystem /tmp"]
    mcp github [url=https://api.githubcopilot.com/mcp/]

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
import re
import shlex
import subprocess
from copy import deepcopy

from .cost import parse_cost
from .interpolation import split_unquoted
from .inventory import parse_ansible_inventory
from .mcp import MCPServer, split_command
from .models import (
    ARTIFACT_CONTRACT_TYPES,
    MAX_FALLBACK_LLMS,
    MAX_FANOUT_LLMS,
    MAX_LLM_RETRIES,
    MAX_LLM_TOKENS,
    MAX_REPAIR_ATTEMPTS,
    Agent,
    DockerConfig,
    Function,
    HostGroup,
    LLMProvider,
    Promptfile,
    Task,
    TaskArgument,
    TaskOptions,
    TaskStep,
    _evaluate_expression,
    parse_duration_seconds,
)
from .staleness import split_patterns


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
    "private",
    "confirm",
    "no-cd",
    "no_cd",
    "no-exit-message",
    "no_exit_message",
    "no-quiet",
    "no_quiet",
    "positional-arguments",
    "positional_arguments",
    "ssh-parallel",
    "ssh_parallel",
    "sandbox-read-only",
    "sandbox_read_only",
    "default",
    "script",
    "metadata",
    "env",
    # OS-specific attributes (Justfile-compatible)
    "linux",
    "macos",
    "windows",
    "unix",
}


def _split_option_items(raw: str) -> list[str]:
    """Split an option list on commas, ignoring commas in quotes and parens."""
    items: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    for ch in raw:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            continue
        if ch == "(":
            depth += 1
            current.append(ch)
            continue
        if ch == ")" and depth:
            depth -= 1
            current.append(ch)
            continue
        if ch == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue
        current.append(ch)
    item = "".join(current).strip()
    if item:
        items.append(item)
    return items


def _parse_function_attr(raw: str) -> tuple[str, str | None]:
    """Parse Just-style attr(args) items used in task option brackets."""
    idx = raw.find("(")
    if idx == -1 or not raw.endswith(")"):
        return raw, None
    return raw[:idx].strip(), raw[idx + 1 : -1].strip()


def _split_function_args(raw: str) -> list[str]:
    return [_strip_quotes(item.strip()) for item in _split_option_items(raw)]


def _parse_kv_pairs(raw: str) -> dict[str, str]:
    """Parse 'key=value, key2=value2, bare_flag' into a dict.

    Bare keywords (no ``=``) are allowed for known boolean options;
    they are stored as ``key -> "true"``.
    """
    result: dict[str, str] = {}
    for pair in _split_option_items(raw):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            attr_name, attr_args = _parse_function_attr(pair)
            if attr_name == "confirm" and attr_args is not None:
                args = _split_function_args(attr_args)
                if len(args) != 1:
                    raise ParseError("confirm(...) expects one message")
                result["confirm"] = args[0]
            elif attr_name == "env" and attr_args is not None:
                args = _split_function_args(attr_args)
                if len(args) != 2:
                    raise ParseError("env(...) expects a name and value")
                result[f"env:{args[0]}"] = args[1]
            elif attr_name == "extension" and attr_args is not None:
                args = _split_function_args(attr_args)
                if len(args) != 1:
                    raise ParseError("extension(...) expects one value")
                result["extension"] = args[0]
            elif attr_name == "script" and attr_args is not None:
                args = _split_function_args(attr_args)
                if len(args) != 1:
                    raise ParseError("script(...) expects one command")
                result["script"] = "true"
                result["script_command"] = args[0]
            elif pair in _BOOLEAN_OPTIONS:
                result[pair] = "true"
            else:
                raise ParseError(f"invalid option (expected key=value): {pair!r}")
        else:
            key, value = pair.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _parse_max_concurrency(opts: dict[str, str], lineno: int | None = None) -> int | None:
    """Parse a provider's concurrency cap."""
    for key in ("max-concurrency", "max_concurrency"):
        if key not in opts:
            continue
        try:
            limit = int(_strip_quotes(opts[key]))
        except ValueError:
            raise ParseError(f"{key} must be an integer, got {opts[key]!r}", lineno)
        if limit < 1:
            raise ParseError(f"{key} must be at least 1", lineno)
        return limit
    return None


def _parse_price(
    opts: dict[str, str],
    keys: tuple[str, ...],
    lineno: int | None = None,
) -> float | None:
    """Parse a provider price in USD per million tokens."""
    for key in keys:
        if key not in opts:
            continue
        try:
            price = float(_strip_quotes(opts[key]))
        except ValueError:
            raise ParseError(f"{key} must be a number, got {opts[key]!r}", lineno)
        if price < 0:
            raise ParseError(f"{key} must not be negative", lineno)
        return price
    return None


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
        elif key in ("max_tokens", "max-tokens"):
            try:
                opts.max_tokens = int(value)
            except ValueError:
                raise ParseError(f"max_tokens must be an integer, got {value!r}")
            if not 1 <= opts.max_tokens <= MAX_LLM_TOKENS:
                raise ParseError(f"max_tokens must be between 1 and {MAX_LLM_TOKENS}")
        elif key == "llm":
            providers = [name.strip() for name in _strip_quotes(value).split("|") if name.strip()]
            if not providers:
                raise ParseError("llm requires at least one provider name")
            if len(providers) > MAX_FANOUT_LLMS:
                raise ParseError(f"llm accepts at most {MAX_FANOUT_LLMS} fan-out providers")
            opts.llm = providers[0]
            opts.llms = providers
        elif key == "judge":
            opts.judge = _strip_quotes(value)
        elif key == "mcp":
            servers = [
                name.strip()
                for name in _strip_quotes(value).replace("|", ",").split(",")
                if name.strip()
            ]
            if not servers:
                raise ParseError("mcp requires at least one server name")
            opts.mcp.extend(servers)
        elif key == "agent":
            opts.agent = value
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
        elif key == "when":
            opts.when.append(value)
        elif key == "register":
            opts.register = value
        elif key == "webhook":
            opts.webhook = value
        elif key in ("webhook-on", "webhook_on"):
            if value not in ("always", "success", "failure"):
                raise ParseError(
                    f"webhook_on must be 'always', 'success', or 'failure', got {value!r}"
                )
            opts.webhook_on = value
        elif key == "cache":
            opts.cache = value
        elif key in ("sources", "source"):
            opts.sources.extend(split_patterns(_strip_quotes(value)))
            if not opts.sources:
                raise ParseError("sources requires at least one file pattern")
        elif key in ("outputs", "output"):
            opts.outputs.extend(split_patterns(_strip_quotes(value)))
            if not opts.outputs:
                raise ParseError("outputs requires at least one file pattern")
        elif key == "timeout":
            try:
                parse_duration_seconds(value)
            except ValueError as e:
                raise ParseError(str(e))
            opts.timeout = value
        elif key in ("llm-timeout", "llm_timeout"):
            try:
                parse_duration_seconds(value)
            except ValueError as e:
                raise ParseError(str(e))
            opts.llm_timeout = value
        elif key == "rollback":
            opts.rollback = value
        elif key in ("postmortem", "on-failure", "on_failure"):
            opts.postmortem = value
        elif key in ("fallback-llm", "fallback_llm"):
            opts.fallback_llms = list(
                dict.fromkeys(
                    name.strip()
                    for name in re.split(r"[| ]+", _strip_quotes(value))
                    if name.strip()
                )
            )
            if len(opts.fallback_llms) > MAX_FALLBACK_LLMS:
                raise ParseError(f"fallback-llm accepts at most {MAX_FALLBACK_LLMS} providers")
        elif key == "retries":
            try:
                opts.retries = int(value)
            except ValueError:
                raise ParseError(f"retries must be an integer, got {value!r}")
            if not 0 <= opts.retries <= MAX_LLM_RETRIES:
                raise ParseError(f"retries must be between 0 and {MAX_LLM_RETRIES}")
        elif key == "requires":
            opts.requires = [
                contract.strip()
                for contract in re.split(r"[| ]+", _strip_quotes(value))
                if contract.strip()
            ]
            for contract in opts.requires:
                reference, separator, suffix = contract.rpartition(":")
                if separator and "." not in suffix:
                    if suffix.lower() not in ARTIFACT_CONTRACT_TYPES:
                        raise ParseError(f"unknown artifact contract type: {suffix!r}")
                else:
                    reference = contract
                artifact, dot, field_name = reference.rpartition(".")
                if not dot or not artifact or not field_name:
                    raise ParseError(
                        f"invalid artifact contract {contract!r}; expected artifact.field[:type]"
                    )
        elif key == "produces":
            output_type = _strip_quotes(value).lower()
            if output_type not in ARTIFACT_CONTRACT_TYPES:
                raise ParseError(f"unknown produces type: {output_type!r}")
            opts.produces = output_type
        elif key == "repair":
            try:
                opts.repair = int(value)
            except ValueError:
                raise ParseError(f"repair must be an integer, got {value!r}")
            if not 0 <= opts.repair <= MAX_REPAIR_ATTEMPTS:
                raise ParseError(f"repair must be between 0 and {MAX_REPAIR_ATTEMPTS}")
        elif key in ("max-cost", "max_cost", "budget"):
            try:
                parse_cost(_strip_quotes(value))
            except ValueError as e:
                raise ParseError(str(e))
            opts.max_cost = _strip_quotes(value)
        elif key == "secrets":
            opts.secrets = value
        elif key in ("ssh-key", "ssh_key", "identity-file", "identity_file"):
            opts.ssh_identity = value
        elif key in ("ssh-strict-host-key-checking", "ssh_strict_host_key_checking"):
            if value not in ("yes", "no", "accept-new"):
                raise ParseError(
                    f"ssh_strict_host_key_checking must be 'yes', 'no', or 'accept-new', got {value!r}"
                )
            opts.ssh_strict_host_key_checking = value
        elif key in ("ssh-parallel", "ssh_parallel"):
            opts.ssh_parallel = value.lower() in ("true", "yes", "1")
        elif key == "sandbox":
            if value not in ("docker", "systemd", "bwrap", "none"):
                raise ParseError(
                    f"sandbox must be 'docker', 'systemd', 'bwrap', or 'none', got {value!r}"
                )
            opts.sandbox = value
        elif key in ("sandbox-image", "sandbox_image"):
            opts.sandbox_image = value
        elif key in ("sandbox-mount", "sandbox_mount"):
            opts.sandbox_mount = value
        elif key in ("sandbox-net", "sandbox_net"):
            if value not in ("none", "host"):
                raise ParseError(f"sandbox_net must be 'none' or 'host', got {value!r}")
            opts.sandbox_net = value
        elif key in ("sandbox-read-only", "sandbox_read_only"):
            opts.sandbox_read_only = value.lower() in ("true", "yes", "1")
        elif key == "default":
            opts.default = value.lower() in ("true", "yes", "1")
        elif key == "script":
            opts.script = value.lower() in ("true", "yes", "1")
        elif key == "script_command":
            opts.script = True
            opts.script_command = _strip_quotes(value)
        elif key == "metadata":
            opts.metadata = value.lower() in ("true", "yes", "1")
        elif key == "env":
            opts.env_enabled = value.lower() in ("true", "yes", "1")
        elif key == "extension":
            opts.extension = _strip_quotes(value)
        elif key.startswith("env:"):
            env_name = key[4:].strip()
            if not env_name:
                raise ParseError("env(...) requires a non-empty name")
            opts.env[env_name] = _strip_quotes(value)
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


def _split_dependencies(raw: str) -> tuple[list[str], list[str]]:
    """Split normal and subsequent dependencies from ``a b && c d``."""
    before, sep, after = raw.partition("&&")
    dependencies = before.split() if before.strip() else []
    subsequent = after.split() if sep and after.strip() else []
    return dependencies, subsequent


def _merge_promptfile(target: Promptfile, included: Promptfile) -> None:
    """Merge included Promptfile definitions into target without overriding local state."""
    for var_name, var_value in included.variables.items():
        if var_name not in target.variables:
            target.variables[var_name] = var_value
            target.included_variables.add(var_name)
    for fn_name, fn in included.functions.items():
        target.functions.setdefault(fn_name, fn)
    for name in included.task_order:
        if name not in target.tasks:
            target.tasks[name] = included.tasks[name]
            target.task_order.append(name)
    for provider_name, provider in included.llm_providers.items():
        target.llm_providers.setdefault(provider_name, provider)
    if included.default_llm and not target.default_llm:
        target.default_llm = included.default_llm
    for group_name, group in included.host_groups.items():
        target.host_groups.setdefault(group_name, group)
    for agent_name, agent in included.agents.items():
        target.agents.setdefault(agent_name, agent)
    if included.guidance and not target.guidance:
        target.guidance = included.guidance
    target.exported_vars.update(included.exported_vars)
    for k, v in included.aliases.items():
        target.aliases.setdefault(k, v)
    if included.settings.default and not target.settings.default:
        target.settings.default = included.settings.default


def _merge_module(target: Promptfile, module_name: str, included: Promptfile) -> None:
    """Merge an included Promptfile as an isolated task namespace."""
    name_map = {name: f"{module_name}::{name}" for name in included.task_order}
    function_map = {name: f"{module_name}::{name}" for name in included.functions}
    provider_map = {name: f"{module_name}::{name}" for name in included.llm_providers}
    host_map = {name: f"{module_name}::{name}" for name in included.host_groups}
    agent_map = {name: f"{module_name}::{name}" for name in included.agents}

    for old_name in included.task_order:
        task = deepcopy(included.tasks[old_name])
        new_name = name_map[old_name]
        task.name = new_name
        task.dependencies = [name_map.get(dep, dep) for dep in task.dependencies]
        task.subsequent_dependencies = [
            name_map.get(dep, dep) for dep in task.subsequent_dependencies
        ]
        if task.options.rollback:
            task.options.rollback = name_map.get(
                task.options.rollback,
                task.options.rollback,
            )
        if task.options.postmortem:
            task.options.postmortem = name_map.get(
                task.options.postmortem,
                task.options.postmortem,
            )
        if task.options.llm:
            task.options.llm = provider_map.get(task.options.llm, task.options.llm)
        task.options.llms = [provider_map.get(name, name) for name in task.options.llms]
        if task.options.judge:
            task.options.judge = provider_map.get(task.options.judge, task.options.judge)
        elif included.default_llm:
            task.options.llm = provider_map[included.default_llm]
        task.options.fallback_llms = [
            provider_map.get(provider, provider) for provider in task.options.fallback_llms
        ]
        if task.options.on:
            task.options.on = host_map.get(task.options.on, task.options.on)
        if task.options.agent:
            task.options.agent = agent_map.get(
                task.options.agent,
                task.options.agent,
            )
        task.local_variables = {
            **included.variables,
            **task.local_variables,
        }
        task.function_namespace = (
            f"{module_name}::{task.function_namespace}" if task.function_namespace else module_name
        )
        if included.guidance:
            task.guidance = "\n\n".join(
                value for value in (included.guidance, task.guidance) if value
            )
        target.tasks[new_name] = task
        target.task_order.append(new_name)

    for fn_name, fn in included.functions.items():
        function = deepcopy(fn)
        function.name = function_map[fn_name]
        target.functions.setdefault(function_map[fn_name], function)
    for provider_name, provider in included.llm_providers.items():
        target.llm_providers.setdefault(provider_map[provider_name], deepcopy(provider))
    for group_name, host_group in included.host_groups.items():
        group = deepcopy(host_group)
        group.name = host_map[group_name]
        target.host_groups.setdefault(host_map[group_name], group)
    for agent_name, source_agent in included.agents.items():
        agent = deepcopy(source_agent)
        agent.name = agent_map[agent_name]
        if agent.llm:
            agent.llm = provider_map.get(agent.llm, agent.llm)
        target.agents.setdefault(agent_map[agent_name], agent)
    for alias_name, alias_target in included.aliases.items():
        target.aliases.setdefault(
            f"{module_name}::{alias_name}",
            name_map.get(alias_target, alias_target),
        )


def _parse_include_path(rest: str, lineno: int, directive: str) -> str:
    if not (
        (rest.startswith('"') and rest.endswith('"'))
        or (rest.startswith("'") and rest.endswith("'"))
    ):
        raise ParseError(f"{directive} path must be quoted: {rest!r}", lineno)
    return rest[1:-1]


def _parse_arguments(raw: str) -> list[TaskArgument]:
    """Parse the inside of (arg1, arg2='default', +variadic) into TaskArgument list."""
    raw = raw.strip()
    if not raw:
        return []
    args: list[TaskArgument] = []
    for part in _split_option_items(raw):
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
            default = _strip_quotes(default.strip())
            args.append(TaskArgument(name=name, default=default, variadic=variadic))
        else:
            args.append(TaskArgument(name=part.strip(), variadic=variadic))
    seen: set[str] = set()
    for index, arg in enumerate(args):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", arg.name):
            raise ParseError(f"invalid task argument name: {arg.name!r}")
        if arg.name in seen:
            raise ParseError(f"duplicate task argument: {arg.name!r}")
        if arg.variadic and index != len(args) - 1:
            raise ParseError("variadic task argument must be last")
        seen.add(arg.name)
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


_STEP_CAPTURE_RE = re.compile(r"^(?P<cmd>.+?)\s+->\s+(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*$")


def _extract_step_routing(command: str) -> tuple[str, str | None, bool, str | None]:
    """Return command text plus optional capture name and pipe prompt."""
    pipe_prompt: str | None = None
    pipe_output = False
    if "|>" in command:
        before, after = command.rsplit("|>", 1)
        command = before.rstrip()
        pipe_output = True
        pipe_prompt = after.strip() or None

    capture: str | None = None
    match = _STEP_CAPTURE_RE.match(command)
    if match:
        command = match.group("cmd").rstrip()
        capture = match.group("name")

    return command, capture, pipe_output, pipe_prompt


def _parse_body_steps(raw_lines: list[str]) -> list[TaskStep]:
    """Parse indented body lines into TaskSteps.

    Lines starting with ! become shell steps.
    Consecutive non-shell lines merge into a single prompt step.
    """
    steps: list[TaskStep] = []
    prompt_accum: list[str] = []
    # Set by an "@llm <name>" line; applies to the prompt steps that follow it.
    step_llm: str | None = None
    pipe_next = False

    def flush_prompt() -> None:
        nonlocal pipe_next
        if prompt_accum:
            text = "\n".join(prompt_accum).strip()
            if text:
                steps.append(
                    TaskStep(
                        kind="prompt",
                        content=text,
                        llm=step_llm,
                        pipe_output=pipe_next,
                    )
                )
            prompt_accum.clear()
        pipe_next = False

    for raw in raw_lines:
        stripped = raw.strip()
        if stripped.startswith("@llm ") or stripped == "@llm":
            flush_prompt()
            name = stripped[5:].strip() if stripped.startswith("@llm ") else ""
            step_llm = _strip_quotes(name) or None
            continue
        if stripped.endswith("|>") and not stripped.startswith("!"):
            # A prompt line ending in |> pipes this prompt's answer into the next.
            prompt_accum.append(stripped[:-2].rstrip())
            pipe_next = True
            flush_prompt()
            continue
        if stripped.startswith("@echo "):
            flush_prompt()
            msg = stripped[6:].strip()
            # Strip surrounding quotes if present
            if len(msg) >= 2 and msg[0] == msg[-1] and msg[0] in ('"', "'"):
                msg = msg[1:-1]
            steps.append(TaskStep(kind="echo", content=msg))
        elif stripped.startswith("!"):
            flush_prompt()
            rest = stripped[1:]
            # Parse @-prefixes
            silent = False
            ignore_error = False
            quiet = False
            # Check for bare @ prefix (quiet — suppress echoing)
            if (
                rest.startswith("@")
                and not rest.startswith("@silent")
                and not rest.startswith("@ignore")
            ):
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
            rest, capture, pipe_output, pipe_prompt = _extract_step_routing(rest)
            steps.append(
                TaskStep(
                    kind="shell",
                    content=rest,
                    silent=silent,
                    ignore_error=ignore_error,
                    quiet=quiet,
                    capture=capture,
                    pipe_output=pipe_output,
                )
            )
            if pipe_prompt:
                steps.append(TaskStep(kind="prompt", content=pipe_prompt))
        else:
            prompt_accum.append(stripped)

    flush_prompt()
    return steps


def _parse_just_body_steps(raw_lines: list[str]) -> list[TaskStep]:
    """Parse a Just-style recipe body where plain lines are shell commands."""
    nonblank = [raw.strip() for raw in raw_lines if raw.strip()]
    if nonblank and nonblank[0].startswith("#!"):
        return [TaskStep(kind="shell", content="\n".join(nonblank), script=True)]

    steps: list[TaskStep] = []
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue

        quiet = False
        ignore_error = False

        while stripped.startswith(("@", "-")):
            if stripped.startswith("@"):
                quiet = True
                stripped = stripped[1:].lstrip()
            elif stripped.startswith("-"):
                ignore_error = True
                stripped = stripped[1:].lstrip()

        if stripped:
            stripped, capture, _pipe_output, _pipe_prompt = _extract_step_routing(stripped)
            steps.append(
                TaskStep(
                    kind="shell",
                    content=stripped,
                    ignore_error=ignore_error,
                    quiet=quiet,
                    capture=capture,
                )
            )
    return steps


def _parse_task_header(
    rest: str,
    lineno: int,
) -> tuple[str, list[TaskArgument], list[str], list[str], TaskOptions]:
    """Parse a makethlm task header body into name, args, deps, and options."""
    rest, has_colon = _strip_trailing_colon(rest)
    if not has_colon:
        raise ParseError("task header must end with ':'", lineno)

    # Extract arguments (...)
    # Only match parens that appear BEFORE any bracket [
    arguments: list[TaskArgument] = []
    bracket_start = rest.find("[")
    paren_start = rest.find("(")
    if paren_start != -1 and (bracket_start == -1 or paren_start < bracket_start):
        paren_end = rest.find(")", paren_start)
        if paren_end == -1:
            raise ParseError("unclosed parenthesis in task header", lineno)
        arguments = _parse_arguments(rest[paren_start + 1 : paren_end])
        rest = rest[:paren_start] + rest[paren_end + 1 :]

    # Extract -> artifact_name (arrow syntax for register)
    arrow_register: str | None = None
    arrow_idx = rest.find("->")
    if arrow_idx != -1:
        # Everything after -> and before the next : or [ is the artifact name
        after_arrow = rest[arrow_idx + 2 :]
        rest = rest[:arrow_idx]
        # The artifact name may be followed by : (deps) or [ (options)
        # Find the next meaningful delimiter
        artifact_name = after_arrow.strip()
        # Separate from deps/options if present
        for delim_ch in (":", "["):
            delim_pos = artifact_name.find(delim_ch)
            if delim_pos != -1:
                after_arrow_rest = artifact_name[delim_pos:]
                artifact_name = artifact_name[:delim_pos].strip()
                rest = rest.rstrip() + after_arrow_rest
                break
        arrow_register = artifact_name if artifact_name else None

    # Extract [options]
    rest, bracket = _extract_brackets(rest)
    options = _parse_task_options(_parse_kv_pairs(bracket)) if bracket else TaskOptions()

    # Apply arrow register to options
    if arrow_register and not options.register:
        options.register = arrow_register

    # What remains: "name" or "name: dep1 dep2"
    rest = rest.strip()
    deps: list[str] = []
    subsequent_deps: list[str] = []
    if ":" in rest:
        colon_idx = rest.index(":")
        task_name = rest[:colon_idx].strip()
        dep_str = rest[colon_idx + 1 :].strip()
        if dep_str:
            deps, subsequent_deps = _split_dependencies(dep_str)
    else:
        task_name = rest.strip()

    if not task_name:
        raise ParseError("task declaration missing name", lineno)

    return task_name, arguments, deps, subsequent_deps, options


def _parse_just_recipe_header(
    rest: str,
    lineno: int,
) -> tuple[str, list[TaskArgument], list[str], list[str], TaskOptions] | None:
    """Parse a bare Just-style recipe header, if the line looks like one."""
    if ":" not in rest:
        return None
    if rest.startswith((" ", "\t", "#")):
        return None
    if any(ch in rest for ch in "{}"):
        return None

    rest, bracket = _extract_brackets(rest)
    options = _parse_task_options(_parse_kv_pairs(bracket)) if bracket else TaskOptions()

    header, dep_str = rest.split(":", 1)
    dep_str = dep_str.strip()
    if dep_str.endswith(":"):
        dep_str = dep_str[:-1].strip()

    parts = header.split()
    if not parts:
        return None
    task_name = parts[0]
    if task_name in {
        "agent",
        "alias",
        "docker",
        "export",
        "fn",
        "guidance",
        "hosts",
        "include",
        "import",
        "import?",
        "inventory",
        "llm",
        "mod",
        "set",
        "task",
    }:
        return None

    arguments: list[TaskArgument] = []
    if len(parts) > 1:
        arguments = _parse_arguments(",".join(parts[1:]))

    deps, subsequent_deps = _split_dependencies(dep_str)
    return task_name, arguments, deps, subsequent_deps, options


def _strip_quotes(val: str) -> str:
    """Strip surrounding single or double quotes."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def _resolve_set_value(
    raw: str, pf: Promptfile, lineno: int, *, allow_backticks: bool = True
) -> str:
    """Resolve a set-directive string value.

    Supports the same expressions as variable declarations (quoted strings,
    backtick commands, concatenation with ``+``, if/else, variable references)
    while also accepting bare unquoted tokens like ``docker`` or ``bash``.
    """
    try:
        return _parse_var_value(raw, pf, lineno, allow_backticks=allow_backticks)
    except ParseError:
        # Only plain tokens may fall back to literal values. Failed command
        # substitutions and malformed expressions remain parse errors.
        value = raw.strip()
        if re.fullmatch(r"[A-Za-z0-9_./:~+-]+", value):
            return value
        raise


def _parse_string_array(raw: str, lineno: int) -> list[str]:
    """Parse a simple Just-style string array like ["bash", "-cu"]."""
    value = raw.strip()
    if value.startswith(":="):
        value = value[2:].strip()
    if not (value.startswith("[") and value.endswith("]")):
        raise ParseError(f"expected string array, got {raw!r}", lineno)
    items = _split_option_items(value[1:-1])
    result = [_strip_quotes(item.strip()) for item in items if item.strip()]
    if not result:
        raise ParseError("shell array must not be empty", lineno)
    return result


def _parse_set_directive(
    rest: str, pf: Promptfile, lineno: int, *, allow_backticks: bool = True
) -> None:
    """Parse a 'set <directive> [value]' line."""
    # Map directive names to settings fields
    directives = {
        "dotenv-load": "dotenv_load",
        "dotenv-path": "dotenv_path",
        "dotenv-required": "dotenv_required",
        "secrets": "secrets",
        "secrets-project": "secrets_project",
        "secrets-environment": "secrets_environment",
        "secrets-vault": "secrets_vault",
        "secrets-file": "secrets_file",
        "shell": "shell",
        "working-dir": "working_dir",
        "working_dir": "working_dir",
        "export": "export",
        "positional-arguments": "positional_arguments",
        "positional_arguments": "positional_arguments",
        "ignore-comments": "ignore_comments",
        "ignore_comments": "ignore_comments",
        "tempdir": "tempdir",
        "quiet": "quiet",
        "allow-duplicate-tasks": "allow_duplicate_tasks",
        "allow-duplicate-variables": "allow_duplicate_variables",
        "default": "default",
        "sandbox": "sandbox",
        "secrets-audit": "secrets_audit",
        "allow-secrets-in-prompts": "allow_secrets_in_prompts",
        "secrets-allow-llm": "allow_secrets_in_prompts",
    }

    # Find which directive matches
    matched_key = None
    matched_field = None
    for key, field_name in directives.items():
        if rest.startswith(key):
            # Make sure it's a complete word match
            after = rest[len(key) :]
            if not after or after[0] in (" ", "\t"):
                matched_key = key
                matched_field = field_name
                break

    if matched_key is None or matched_field is None:
        raise ParseError(f"unknown set directive: {rest!r}", lineno)

    raw_val = rest[len(matched_key) :].strip()
    if matched_field == "shell" and raw_val.lstrip().startswith(":="):
        pf.settings.shell_argv = _parse_string_array(raw_val, lineno)
        pf.settings.shell = pf.settings.shell_argv[0]
        return

    stripped_val = _strip_quotes(raw_val)

    _BOOL_VALUES = {"true", "false", "yes", "no", "0", "1"}

    # Pure boolean directives: bare ``set X`` means True, ``set X false`` means False
    bool_fields = {
        "dotenv_required",
        "export",
        "positional_arguments",
        "ignore_comments",
        "quiet",
        "allow_duplicate_tasks",
        "allow_duplicate_variables",
        "secrets_audit",
        "allow_secrets_in_prompts",
    }

    # Optional-bool directives: boolean when bare or given true/false,
    # but also accept a string value that implies True + sets a companion field.
    # Maps field -> companion field that receives the string value.
    optional_bool_fields = {
        "dotenv_load": "dotenv_path",
    }

    if matched_field in optional_bool_fields:
        if stripped_val and stripped_val.lower() not in _BOOL_VALUES:
            resolved = _resolve_set_value(raw_val, pf, lineno, allow_backticks=allow_backticks)
            setattr(pf.settings, matched_field, True)
            setattr(pf.settings, optional_bool_fields[matched_field], resolved)
        else:
            bool_val = stripped_val.lower() not in ("false", "no", "0") if stripped_val else True
            setattr(pf.settings, matched_field, bool_val)
    elif matched_field in bool_fields:
        bool_val = stripped_val.lower() not in ("false", "no", "0") if stripped_val else True
        setattr(pf.settings, matched_field, bool_val)
    else:
        # String directives: resolve variables, concatenation, backticks, etc.
        if raw_val:
            resolved = _resolve_set_value(raw_val, pf, lineno, allow_backticks=allow_backticks)
            if matched_field == "sandbox" and resolved not in (
                "docker",
                "systemd",
                "bwrap",
                "none",
            ):
                raise ParseError(
                    "sandbox must be 'docker', 'systemd', 'bwrap', or 'none'",
                    lineno,
                )
            setattr(pf.settings, matched_field, resolved)
        else:
            setattr(pf.settings, matched_field, None)
        # Setting dotenv-path implicitly enables dotenv-load
        if matched_field == "dotenv_path" and raw_val:
            pf.settings.dotenv_load = True


def _assign_variable(
    pf: Promptfile,
    name: str,
    value: str,
    lineno: int,
    *,
    exported: bool = False,
) -> None:
    """Validate and assign a Promptfile variable."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
        raise ParseError(f"invalid variable name: {name!r}", lineno)
    imported_value = name in pf.included_variables
    if name in pf.variables and not imported_value and not pf.settings.allow_duplicate_variables:
        raise ParseError(f"duplicate variable: {name!r}", lineno)
    pf.variables[name] = value
    pf.included_variables.discard(name)
    if exported:
        pf.exported_vars.add(name)


# ---------------------------------------------------------------------------
# Main parser — line scanner, no regex on hot path
# ---------------------------------------------------------------------------


def parse(
    source: str,
    filename: str = "Promptfile",
    *,
    _included: set[str] | None = None,
    _base_dir: str | None = None,
    allow_backticks: bool = True,
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

        # ----- include/import "path"; import? "path" -----
        include_directive: str | None = None
        optional_include = False
        if stripped.startswith("include "):
            include_directive = "include"
            rest = stripped[8:].strip()
        elif stripped.startswith("import? "):
            include_directive = "import?"
            optional_include = True
            rest = stripped[8:].strip()
        elif stripped.startswith("import "):
            include_directive = "import"
            rest = stripped[7:].strip()
        if include_directive:
            include_path = _parse_include_path(rest, lineno, include_directive)
            resolved = os.path.normpath(os.path.join(_base_dir, include_path))
            if resolved in _included:
                raise ParseError(f"circular {include_directive}: {include_path!r}", lineno)
            try:
                with open(resolved) as f:
                    include_source = f.read()
            except FileNotFoundError:
                if optional_include:
                    i += 1
                    continue
                raise ParseError(f"{include_directive} file not found: {include_path!r}", lineno)
            included_pf = parse(
                include_source,
                resolved,
                _included=_included,
                _base_dir=os.path.dirname(resolved),
                allow_backticks=allow_backticks,
            )
            _merge_promptfile(pf, included_pf)
            i += 1
            continue

        # ----- mod <name> ["path"] -----
        if stripped.startswith("mod "):
            rest = stripped[4:].strip()
            parts = rest.split(None, 1)
            module_name = parts[0] if parts else ""
            if not module_name:
                raise ParseError("mod declaration missing name", lineno)
            if len(parts) > 1:
                module_path = _parse_include_path(parts[1].strip(), lineno, "mod")
            else:
                module_path = f"{module_name}.pf"
            resolved = os.path.normpath(os.path.join(_base_dir, module_path))
            if resolved in _included:
                raise ParseError(f"circular mod: {module_path!r}", lineno)
            try:
                with open(resolved) as f:
                    module_source = f.read()
            except FileNotFoundError:
                raise ParseError(f"mod file not found: {module_path!r}", lineno)
            module_pf = parse(
                module_source,
                resolved,
                _included=_included,
                _base_dir=os.path.dirname(resolved),
                allow_backticks=allow_backticks,
            )
            _merge_module(pf, module_name, module_pf)
            i += 1
            continue

        # ----- set <directive> [value] -----
        if stripped.startswith("set "):
            _parse_set_directive(stripped[4:].strip(), pf, lineno, allow_backticks=allow_backticks)
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
                var_val = _parse_var_value(var_val_raw, pf, lineno, allow_backticks=allow_backticks)
                _assign_variable(pf, var_name, var_val, lineno, exported=True)
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
            var_val = _parse_var_value(var_val_raw, pf, lineno, allow_backticks=allow_backticks)
            _assign_variable(pf, var_name, var_val, lineno)
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
                api_key = os.environ.get(api_key[1:])
            provider = LLMProvider(
                name=llm_name,
                model=llm_opts.get("model"),
                api_key=api_key,
                base_url=llm_opts.get("base_url"),
                shell_template=llm_opts.get("template"),
                price_in=_parse_price(llm_opts, ("price-in", "price_in"), lineno),
                price_out=_parse_price(llm_opts, ("price-out", "price_out"), lineno),
                max_concurrency=_parse_max_concurrency(llm_opts, lineno),
            )
            pf.llm_providers[llm_name] = provider
            if pf.default_llm is None:
                pf.default_llm = llm_name
            i += 1
            continue

        # ----- mcp <name> [opts] -----
        if stripped.startswith("mcp "):
            rest = stripped[4:].strip()
            rest, bracket = _extract_brackets(rest)
            mcp_name = rest.strip()
            if not mcp_name:
                raise ParseError("mcp declaration missing name", lineno)
            mcp_opts = _parse_kv_pairs(bracket) if bracket else {}
            url = _strip_quotes(mcp_opts.get("url", "")) or None
            raw_command = _strip_quotes(mcp_opts.get("command", "")) or None
            if url and raw_command:
                raise ParseError(
                    f"mcp {mcp_name!r} sets both url and command; pick one",
                    lineno,
                )
            if not url and not raw_command:
                raise ParseError(
                    f"mcp {mcp_name!r} needs either command=... or url=...",
                    lineno,
                )
            command: str | None = None
            args: list[str] = []
            if raw_command:
                try:
                    command, args = split_command(raw_command)
                except ValueError as e:
                    raise ParseError(f"mcp {mcp_name!r}: {e}", lineno)
                extra = _strip_quotes(mcp_opts.get("args", ""))
                if extra:
                    args.extend(shlex.split(extra))
            env: dict[str, str] = {}
            for key, value in mcp_opts.items():
                if key.startswith("env:"):
                    env_name = key[4:].strip()
                    if not env_name:
                        raise ParseError("env(...) requires a non-empty name", lineno)
                    env[env_name] = _strip_quotes(value)
            pf.mcp_servers[mcp_name] = MCPServer(
                name=mcp_name,
                command=command,
                args=args,
                env=env,
                url=url,
            )
            i += 1
            continue

        # ----- agent <name> "<path>" [opts] -----
        if stripped.startswith("agent "):
            rest = stripped[6:].strip()
            # Extract [options] first
            rest, bracket = _extract_brackets(rest)
            agent_opts = _parse_kv_pairs(bracket) if bracket else {}
            # Parse: name "path"
            parts = rest.strip().split(None, 1)
            if len(parts) < 2:
                raise ParseError("agent requires a name and a quoted path", lineno)
            agent_name = parts[0]
            agent_path_raw = parts[1].strip()
            if not (
                (agent_path_raw.startswith('"') and agent_path_raw.endswith('"'))
                or (agent_path_raw.startswith("'") and agent_path_raw.endswith("'"))
            ):
                raise ParseError(f"agent path must be quoted: {agent_path_raw!r}", lineno)
            agent_path = agent_path_raw[1:-1]
            if agent_name in pf.agents:
                raise ParseError(f"duplicate agent: {agent_name!r}", lineno)
            # Resolve path relative to Promptfile directory
            resolved_path = os.path.normpath(os.path.join(_base_dir, agent_path))
            try:
                with open(resolved_path) as f:
                    agent_instructions = f.read()
            except FileNotFoundError:
                raise ParseError(f"agent file not found: {agent_path!r}", lineno)
            # Validate options
            for k in agent_opts:
                if k not in ("llm", "model"):
                    raise ParseError(f"unknown agent option: {k!r}", lineno)
            pf.agents[agent_name] = Agent(
                name=agent_name,
                instructions_path=resolved_path,
                instructions=agent_instructions.strip(),
                llm=agent_opts.get("llm"),
                model=agent_opts.get("model"),
                line_number=lineno,
            )
            i += 1
            continue

        # ----- guidance "<path>" or guidance: (inline block) -----
        if stripped.startswith("guidance"):
            rest_g = stripped[8:].strip()
            # File reference: guidance "./rules.md"
            if rest_g and rest_g[0] in ('"', "'"):
                if not (
                    (rest_g.startswith('"') and rest_g.endswith('"'))
                    or (rest_g.startswith("'") and rest_g.endswith("'"))
                ):
                    raise ParseError(f"guidance path must be quoted: {rest_g!r}", lineno)
                guidance_path = rest_g[1:-1]
                resolved_g = os.path.normpath(os.path.join(_base_dir, guidance_path))
                try:
                    with open(resolved_g) as f:
                        pf.guidance = f.read().strip()
                except FileNotFoundError:
                    raise ParseError(f"guidance file not found: {guidance_path!r}", lineno)
                i += 1
                continue
            # Inline block: guidance:
            rest_g2, has_colon = _strip_trailing_colon(stripped[8:])
            if not has_colon and not rest_g:
                raise ParseError("guidance must be followed by a quoted path or ':'", lineno)
            if has_colon:
                i += 1
                body_lines, i = _collect_body(lines, i)
                body = "\n".join(body_line.strip() for body_line in body_lines).strip()
                if not body:
                    raise ParseError("guidance block has no body", lineno)
                pf.guidance = body
                continue
            raise ParseError("guidance must be followed by a quoted path or ':'", lineno)

        # ----- inventory "<path>" -----
        if stripped.startswith("inventory "):
            rest = stripped[10:].strip()
            if not (
                (rest.startswith('"') and rest.endswith('"'))
                or (rest.startswith("'") and rest.endswith("'"))
            ):
                raise ParseError(f"inventory path must be quoted: {rest!r}", lineno)
            inv_path = rest[1:-1]
            resolved_inv = os.path.normpath(os.path.join(_base_dir, inv_path))
            try:
                inv_groups = parse_ansible_inventory(resolved_inv)
            except FileNotFoundError:
                raise ParseError(f"inventory file not found: {inv_path!r}", lineno)
            for gname, group in inv_groups.items():
                if gname in pf.host_groups:
                    raise ParseError(
                        f"inventory group {gname!r} conflicts with existing host group",
                        lineno,
                    )
                group.line_number = lineno
                pf.host_groups[gname] = group
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
                if k not in (
                    "user",
                    "port",
                    "identity-file",
                    "identity_file",
                    "ssh-key",
                    "ssh_key",
                    "strict-host-key-checking",
                    "strict_host_key_checking",
                    "ssh-strict-host-key-checking",
                    "ssh_strict_host_key_checking",
                ):
                    raise ParseError(f"unknown host option: {k!r}", lineno)
            if group_name in pf.host_groups:
                raise ParseError(f"duplicate host group: {group_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            host_list = [body_line.strip() for body_line in body_lines if body_line.strip()]
            if not host_list:
                raise ParseError(f"host group {group_name!r} has no hosts", lineno)

            port = None
            if "port" in host_kvs:
                try:
                    port = int(host_kvs["port"])
                except ValueError:
                    raise ParseError("port must be an integer", lineno)

            strict_host_key_checking = (
                host_kvs.get("strict-host-key-checking")
                or host_kvs.get("strict_host_key_checking")
                or host_kvs.get("ssh-strict-host-key-checking")
                or host_kvs.get("ssh_strict_host_key_checking")
            )
            if strict_host_key_checking and strict_host_key_checking not in (
                "yes",
                "no",
                "accept-new",
            ):
                raise ParseError(
                    "strict_host_key_checking must be 'yes', 'no', or 'accept-new'",
                    lineno,
                )

            identity_file = (
                host_kvs.get("identity-file")
                or host_kvs.get("identity_file")
                or host_kvs.get("ssh-key")
                or host_kvs.get("ssh_key")
            )

            pf.host_groups[group_name] = HostGroup(
                name=group_name,
                hosts=host_list,
                user=host_kvs.get("user"),
                port=port,
                identity_file=identity_file,
                strict_host_key_checking=strict_host_key_checking,
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
            body = "\n".join(body_line.strip() for body_line in body_lines).strip()
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
            docker_cfg = (
                _parse_docker_config(_parse_kv_pairs(bracket)) if bracket else DockerConfig()
            )

            if docker_name in pf.tasks:
                raise ParseError(f"duplicate task/docker: {docker_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            description = "\n".join(body_line.strip() for body_line in body_lines).strip()
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
            task_name, arguments, deps, subsequent_deps, options = _parse_task_header(
                stripped[5:], lineno
            )

            # Tasks starting with _ are implicitly private (Justfile convention)
            if task_name.startswith("_"):
                options.private = True

            if task_name in pf.tasks:
                if not pf.settings.allow_duplicate_tasks:
                    raise ParseError(f"duplicate task: {task_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            steps = _parse_body_steps(body_lines)
            if options.script:
                script_text = "\n".join(line.strip() for line in body_lines if line.strip())
                steps = [TaskStep(kind="shell", content=script_text, script=True)]
            if not steps:
                raise ParseError(f"task {task_name!r} has no prompt body", lineno)

            task = Task(
                name=task_name,
                steps=steps,
                dependencies=deps,
                subsequent_dependencies=subsequent_deps,
                options=options,
                arguments=arguments,
                line_number=lineno,
            )
            pf.tasks[task_name] = task
            if task_name not in pf.task_order:
                pf.task_order.append(task_name)
            if options.default:
                if pf.settings.default and pf.settings.default != task_name:
                    raise ParseError(
                        f"multiple default tasks: {pf.settings.default!r} and {task_name!r}", lineno
                    )
                pf.settings.default = task_name
            continue

        # ----- Just-style recipe: name [args]: [deps] -----
        just_recipe = _parse_just_recipe_header(stripped, lineno)
        if just_recipe is not None:
            task_name, arguments, deps, subsequent_deps, options = just_recipe

            if task_name.startswith("_"):
                options.private = True

            if task_name in pf.tasks:
                if not pf.settings.allow_duplicate_tasks:
                    raise ParseError(f"duplicate task: {task_name!r}", lineno)

            i += 1
            body_lines, i = _collect_body(lines, i)
            steps = _parse_just_body_steps(body_lines)
            if options.script:
                script_text = "\n".join(line.strip() for line in body_lines if line.strip())
                steps = [TaskStep(kind="shell", content=script_text, script=True)]
            if not steps:
                raise ParseError(f"task {task_name!r} has no shell body", lineno)

            task = Task(
                name=task_name,
                steps=steps,
                dependencies=deps,
                subsequent_dependencies=subsequent_deps,
                options=options,
                arguments=arguments,
                line_number=lineno,
            )
            pf.tasks[task_name] = task
            if task_name not in pf.task_order:
                pf.task_order.append(task_name)
            if options.default:
                if pf.settings.default and pf.settings.default != task_name:
                    raise ParseError(
                        f"multiple default tasks: {pf.settings.default!r} and {task_name!r}", lineno
                    )
                pf.settings.default = task_name
            continue

        raise ParseError(f"unexpected line: {line!r}", lineno)

    # --- Validation ---
    for task in pf.tasks.values():
        for dep in task.dependencies + task.subsequent_dependencies:
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
                        namespaced_fn = (
                            f"{task.function_namespace}::{fn_name}"
                            if task.function_namespace
                            else None
                        )
                        if fn_name not in pf.functions and namespaced_fn not in pf.functions:
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
        # Validate [agent=X] references
        if task.options.agent and task.options.agent not in pf.agents:
            raise ParseError(
                f"task {task.name!r} references unknown agent {task.options.agent!r}",
                task.line_number,
            )
        # Validate rollback hooks
        if task.options.rollback and task.options.rollback not in pf.tasks:
            raise ParseError(
                f"task {task.name!r} rollback targets unknown task {task.options.rollback!r}",
                task.line_number,
            )
        if task.options.postmortem and task.options.postmortem not in pf.tasks:
            raise ParseError(
                f"task {task.name!r} postmortem targets unknown task {task.options.postmortem!r}",
                task.line_number,
            )
        for server_name in task.options.mcp:
            if server_name not in pf.mcp_servers:
                raise ParseError(
                    f"task {task.name!r} references unknown MCP server {server_name!r}",
                    task.line_number,
                )
        task.mcp_servers = [pf.mcp_servers[name] for name in task.options.mcp]
        # Provider names stay lenient at parse time, like [llm=...] always has;
        # --check reports the ones that do not resolve.
        if task.options.judge and len(task.options.llms) < 2:
            raise ParseError(
                f"task {task.name!r} sets judge without a fan-out; "
                'use llm="a|b" to give the judge answers to merge',
                task.line_number,
            )
        for fallback_name in task.options.fallback_llms:
            if fallback_name not in pf.llm_providers:
                raise ParseError(
                    f"task {task.name!r} references unknown fallback LLM provider "
                    f"{fallback_name!r}",
                    task.line_number,
                )

    # Validate alias targets
    for alias_name, alias_target in pf.aliases.items():
        if alias_target not in pf.tasks:
            raise ParseError(f"alias {alias_name!r} targets unknown task {alias_target!r}")

    # Validate explicit default task
    if pf.settings.default and pf.settings.default not in pf.tasks:
        raise ParseError(f"set default references unknown task {pf.settings.default!r}")

    _included.discard(abs_filename)
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


def _parse_var_value(raw: str, pf: Promptfile, lineno: int, *, allow_backticks: bool = True) -> str:
    """Parse a variable value: quoted string, backtick command, or expression."""
    raw = raw.strip()

    # Version literal: v"1.2.3" or v'1.2.3'
    if raw.startswith('v"') and raw.endswith('"'):
        return raw[2:-1]
    if raw.startswith("v'") and raw.endswith("'"):
        return raw[2:-1]

    # Version from command: v`command`
    if raw.startswith("v`") and raw.endswith("`"):
        if not allow_backticks:
            raise ParseError("backtick command substitution is disabled in safe mode", lineno)
        return _run_backtick(raw[2:-1], lineno)

    # Backtick command substitution
    if raw.startswith("`") and raw.endswith("`"):
        if not allow_backticks:
            raise ParseError("backtick command substitution is disabled in safe mode", lineno)
        return _run_backtick(raw[1:-1], lineno)

    # if/else expression (check before concat to avoid splitting on + inside quotes)
    if raw.startswith("if "):
        return _evaluate_expression(raw, pf.variables)

    # String concatenation with +
    parts = split_unquoted(raw, "+")
    if len(parts) > 1:
        result_parts: list[str] = []
        for part in parts:
            part = part.strip()
            if (part.startswith('"') and part.endswith('"')) or (
                part.startswith("'") and part.endswith("'")
            ):
                result_parts.append(part[1:-1])
            elif part in pf.variables:
                result_parts.append(pf.variables[part])
            else:
                result_parts.append(part)
        return "".join(result_parts)

    # Quoted literals may contain a plus sign without becoming concatenations.
    if raw.startswith('"') and raw.endswith('"'):
        val = raw[1:-1]
        return val.replace('\\"', '"').replace("\\\\", "\\")
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]

    raise ParseError(f"variable value must be quoted, backtick, or expression: {raw!r}", lineno)


def _run_backtick(command: str, lineno: int) -> str:
    """Execute an explicit Promptfile command substitution."""
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", command],
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise ParseError(f"backtick command failed: {e}", lineno)
    if proc.returncode != 0:
        raise ParseError(
            f"backtick command exited with status {proc.returncode}",
            lineno,
        )
    return proc.stdout.strip()
