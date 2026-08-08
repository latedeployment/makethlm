"""Data models for the Promptfile AST."""

from __future__ import annotations

import os
import platform
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from . import gitinfo
from .interpolation import (
    apply_parameter_expansion,
    interpolate_text,
    resolve_env_vars,
    split_unquoted,
    try_parameter_expansion,
)
from .mcp import MCPServer
from .secrets import SecretError, is_secret_name, resolve_secret, resolve_secrets

ARTIFACT_CONTRACT_TYPES = frozenset(
    {
        "text",
        "nonempty",
        "json",
        "object",
        "array",
        "integer",
        "number",
        "boolean",
    }
)
MAX_LLM_RETRIES = 10
MAX_FALLBACK_LLMS = 4
MAX_REPAIR_ATTEMPTS = 3
MAX_FANOUT_LLMS = 8
MAX_LLM_TOKENS = 1_000_000


@dataclass
class LLMProvider:
    """An LLM provider configuration.

    Defined globally with ``llm <name>`` or per-task with ``[llm=<name>]``.
    """

    name: str  # e.g. "claude", "openai", "ollama", "shell"
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    shell_template: str | None = None  # for shell provider: 'cmd {prompt}'
    price_in: float | None = None  # USD per million input tokens
    price_out: float | None = None  # USD per million output tokens
    max_concurrency: int | None = None  # cap on simultaneous calls to this provider


@dataclass
class Settings:
    """Global configuration from ``set`` directives (Justfile-compatible)."""

    dotenv_load: bool = False
    dotenv_path: str | None = None  # custom .env path
    dotenv_required: bool = False  # error if .env missing
    secrets: str | None = None  # secrets backend name
    secrets_project: str | None = None  # backend-specific project/name
    secrets_environment: str | None = None
    secrets_vault: str | None = None
    secrets_file: str | None = None
    shell: str | None = None  # shell executable (default: sh)
    shell_argv: list[str] | None = None  # Just-style set shell := ["bash", "-cu"]
    working_dir: str | None = None  # global working directory
    export: bool = False  # export all variables to env
    positional_arguments: bool = False  # pass task args as $1, $2...
    ignore_comments: bool = False  # strip # comments from shell cmds
    tempdir: str | None = None  # temp directory for recipes
    quiet: bool = False  # suppress command echoing globally
    allow_duplicate_tasks: bool = False  # allow redefining tasks
    allow_duplicate_variables: bool = False
    default: str | None = None  # explicit default task name
    sandbox: str | None = None  # global sandbox: docker, systemd, bwrap
    secrets_audit: bool = False  # log secret resolution metadata
    allow_secrets_in_prompts: bool = True  # allow {{#secret:}} in LLM prompts


@dataclass
class TaskOptions:
    """Optional metadata attached to a task via [key=value, ...] syntax."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    llm: str | None = None  # per-task LLM provider override (first of llms)
    llms: list[str] = field(default_factory=list)  # fan-out providers from llm="a|b"
    judge: str | None = None  # provider that merges fan-out answers
    mcp: list[str] = field(default_factory=list)  # MCP servers this task may use
    agent: str | None = None  # agent to use for this task
    on: str | None = None  # host group to run on (Ansible-like)
    private: bool = False  # hide from --list
    group: str | None = None  # group name for --list display
    doc: str | None = None  # one-line description for --list
    confirm: str | bool = False  # True or custom message
    os_filter: str | None = None  # "linux", "macos", "windows", "unix"
    working_dir: str | None = None  # per-task working directory
    no_cd: bool = False  # don't change to working dir
    no_exit_message: bool = False  # suppress error message on failure
    no_quiet: bool = False  # override global quiet for this task
    positional_arguments: bool | None = None  # per-task override
    register: str | None = None  # register task output as named artifact
    webhook: str | None = None  # webhook URL to call on task completion
    webhook_on: str = "always"  # "always", "success", or "failure"
    secrets: str | None = None  # per-task secrets backend override
    when: list[str] = field(default_factory=list)  # conditional execution expressions
    cache: str | None = None  # cache duration e.g. "1h", "30m", "1d"
    sources: list[str] = field(default_factory=list)  # input file globs for staleness
    outputs: list[str] = field(default_factory=list)  # output file globs for staleness
    timeout: str | None = None  # shell/SSH timeout e.g. "30s", "5m"
    llm_timeout: str | None = None  # prompt/LLM timeout e.g. "5m"
    rollback: str | None = None  # task to run if this task fails
    postmortem: str | None = None  # diagnostic task to run after failure
    fallback_llms: list[str] = field(default_factory=list)
    retries: int = 0  # retry count per LLM provider
    requires: list[str] = field(default_factory=list)  # artifact contracts
    produces: str | None = None  # output type contract
    repair: int = 0  # re-prompt attempts when produces is violated
    max_cost: str | None = None  # spend limit in USD for runs reaching this task
    ssh_identity: str | None = None  # SSH identity file for remote shell steps
    ssh_strict_host_key_checking: str | None = None  # yes, no, or accept-new
    ssh_parallel: bool = False  # run each shell step across hosts concurrently
    sandbox: str | None = None  # "docker", "systemd", "bwrap", or "none"
    sandbox_image: str | None = None  # docker image (default: ubuntu:latest)
    sandbox_mount: str | None = None  # extra mount (src:dst)
    sandbox_net: str | None = None  # network mode (e.g., "host", "none")
    sandbox_read_only: bool = False  # mount workspace read-only where supported
    default: bool = False  # Just-style [default] task attribute
    env: dict[str, str] = field(default_factory=dict)  # env vars for shell steps
    script: bool = False  # Just-style [script] attribute
    script_command: str | None = None  # Just-style [script(COMMAND)] interpreter
    extension: str | None = None  # Just-style [extension] metadata
    metadata: bool = False  # Just-style [metadata] marker
    env_enabled: bool = False  # Just-style [env] marker

    def merge(self, overrides: TaskOptions) -> TaskOptions:
        """Return a new TaskOptions with non-None overrides applied."""
        return TaskOptions(
            model=overrides.model if overrides.model is not None else self.model,
            temperature=overrides.temperature
            if overrides.temperature is not None
            else self.temperature,
            max_tokens=overrides.max_tokens
            if overrides.max_tokens is not None
            else self.max_tokens,
            llm=overrides.llm if overrides.llm is not None else self.llm,
            llms=overrides.llms if overrides.llms else self.llms,
            judge=overrides.judge if overrides.judge is not None else self.judge,
            mcp=overrides.mcp if overrides.mcp else self.mcp,
            agent=overrides.agent if overrides.agent is not None else self.agent,
            on=overrides.on if overrides.on is not None else self.on,
            private=overrides.private or self.private,
            group=overrides.group if overrides.group is not None else self.group,
            doc=overrides.doc if overrides.doc is not None else self.doc,
            confirm=overrides.confirm if overrides.confirm else self.confirm,
            os_filter=overrides.os_filter if overrides.os_filter is not None else self.os_filter,
            working_dir=overrides.working_dir
            if overrides.working_dir is not None
            else self.working_dir,
            no_cd=overrides.no_cd or self.no_cd,
            no_exit_message=overrides.no_exit_message or self.no_exit_message,
            no_quiet=overrides.no_quiet or self.no_quiet,
            positional_arguments=overrides.positional_arguments
            if overrides.positional_arguments is not None
            else self.positional_arguments,
            register=overrides.register if overrides.register is not None else self.register,
            webhook=overrides.webhook if overrides.webhook is not None else self.webhook,
            webhook_on=overrides.webhook_on
            if overrides.webhook_on != "always"
            else self.webhook_on,
            secrets=overrides.secrets if overrides.secrets is not None else self.secrets,
            when=overrides.when if overrides.when else self.when,
            cache=overrides.cache if overrides.cache is not None else self.cache,
            sources=overrides.sources if overrides.sources else self.sources,
            outputs=overrides.outputs if overrides.outputs else self.outputs,
            timeout=overrides.timeout if overrides.timeout is not None else self.timeout,
            llm_timeout=overrides.llm_timeout
            if overrides.llm_timeout is not None
            else self.llm_timeout,
            rollback=overrides.rollback if overrides.rollback is not None else self.rollback,
            postmortem=overrides.postmortem
            if overrides.postmortem is not None
            else self.postmortem,
            fallback_llms=overrides.fallback_llms
            if overrides.fallback_llms
            else self.fallback_llms,
            retries=overrides.retries if overrides.retries else self.retries,
            requires=overrides.requires if overrides.requires else self.requires,
            produces=overrides.produces if overrides.produces is not None else self.produces,
            repair=overrides.repair if overrides.repair else self.repair,
            max_cost=overrides.max_cost if overrides.max_cost is not None else self.max_cost,
            ssh_identity=overrides.ssh_identity
            if overrides.ssh_identity is not None
            else self.ssh_identity,
            ssh_strict_host_key_checking=overrides.ssh_strict_host_key_checking
            if overrides.ssh_strict_host_key_checking is not None
            else self.ssh_strict_host_key_checking,
            ssh_parallel=overrides.ssh_parallel or self.ssh_parallel,
            sandbox=overrides.sandbox if overrides.sandbox is not None else self.sandbox,
            sandbox_image=overrides.sandbox_image
            if overrides.sandbox_image is not None
            else self.sandbox_image,
            sandbox_mount=overrides.sandbox_mount
            if overrides.sandbox_mount is not None
            else self.sandbox_mount,
            sandbox_net=overrides.sandbox_net
            if overrides.sandbox_net is not None
            else self.sandbox_net,
            sandbox_read_only=overrides.sandbox_read_only or self.sandbox_read_only,
            default=overrides.default or self.default,
            env={**self.env, **overrides.env},
            script=overrides.script or self.script,
            script_command=overrides.script_command
            if overrides.script_command is not None
            else self.script_command,
            extension=overrides.extension if overrides.extension is not None else self.extension,
            metadata=overrides.metadata or self.metadata,
            env_enabled=overrides.env_enabled or self.env_enabled,
        )

    def should_skip_for_os(self) -> bool:
        """Return True if this task should be skipped on the current OS."""
        if not self.os_filter:
            return False
        current = platform.system().lower()
        os_map = {
            "linux": "linux",
            "macos": "darwin",
            "windows": "windows",
        }
        # "unix" matches both Linux and macOS
        if self.os_filter == "unix":
            return current not in ("linux", "darwin")
        expected = os_map.get(self.os_filter, self.os_filter)
        return current != expected


@dataclass
class TaskStep:
    """A single step within a task body.

    kind="shell"  -- a line starting with !, executed as a subprocess.
    kind="prompt" -- natural-language text sent to an LLM.
    kind="echo"   -- a line starting with @echo, prints a message to stderr.
    """

    kind: Literal["shell", "prompt", "echo"]
    content: str
    silent: bool = False  # @silent -- suppress stdout
    ignore_error: bool = False  # @ignore -- continue on non-zero exit
    quiet: bool = False  # @ prefix -- suppress command echoing
    capture: str | None = None  # -> name -- expose output as {{name.stdout}}
    pipe_output: bool = False  # |> -- prepend output to the next prompt step
    llm: str | None = None  # @llm <name> -- provider override for this step
    script: bool = False  # execute content as a temporary script file


@dataclass
class TaskArgument:
    """A positional argument for a task: task deploy(target, port="8080").

    Supports Justfile-style variadic args:
      +args  -- one or more (required)
      *args  -- zero or more (optional)
    """

    name: str
    default: str | None = None
    variadic: str | None = None  # None, "+", or "*"


@dataclass
class Agent:
    """A named agent backed by an MD file with system instructions.

    Defined with ``agent <name> "<path>" [llm=..., model=...]``.
    When a task uses ``[agent=name]``, the agent's instructions are
    prepended to every prompt step.
    """

    name: str
    instructions_path: str  # path to .md file
    instructions: str  # loaded content of the .md file
    llm: str | None = None  # optional LLM provider override
    model: str | None = None
    line_number: int = 0


@dataclass
class Function:
    """A reusable prompt template defined with ``fn name:``."""

    name: str
    body: str
    line_number: int = 0


@dataclass
class DockerConfig:
    """Metadata for a ``docker`` block."""

    tag: str = "latest"
    context: str = "."
    file: str = "Dockerfile"


@dataclass
class HostConnection:
    """Per-host SSH settings imported from an inventory."""

    user: str | None = None
    port: int | None = None
    identity_file: str | None = None
    strict_host_key_checking: str | None = None


@dataclass
class HostGroup:
    """A named group of hosts for Ansible-like remote execution.

    Defined with ``hosts <name>:`` followed by indented hostnames.
    """

    name: str
    hosts: list[str] = field(default_factory=list)
    user: str | None = None  # SSH user override
    port: int | None = None  # SSH port override
    identity_file: str | None = None  # SSH identity file
    strict_host_key_checking: str | None = None  # yes, no, or accept-new
    timeout: str | None = None  # effective SSH command timeout
    line_number: int = 0
    connections: dict[str, HostConnection] = field(default_factory=dict)


@dataclass
class Task:
    """A single task definition from a Promptfile."""

    name: str
    steps: list[TaskStep] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    subsequent_dependencies: list[str] = field(default_factory=list)
    options: TaskOptions = field(default_factory=TaskOptions)
    arguments: list[TaskArgument] = field(default_factory=list)
    docker: DockerConfig | None = None
    line_number: int = 0
    local_variables: dict[str, str] = field(default_factory=dict)
    function_namespace: str | None = None
    guidance: str | None = None
    # Resolved from options.mcp by the parser, so dispatchers need only the task.
    mcp_servers: list[MCPServer] = field(default_factory=list)

    @property
    def prompt(self) -> str:
        """Concatenate all prompt steps (for backward compat & simple access)."""
        return "\n".join(s.content for s in self.steps if s.kind == "prompt")

    @property
    def has_shell_steps(self) -> bool:
        return any(s.kind == "shell" for s in self.steps)


# ---------------------------------------------------------------------------
# Built-in functions (Justfile-compatible)
# ---------------------------------------------------------------------------


def _builtin_functions() -> dict[str, str]:
    """Evaluate all built-in functions and return name -> value mapping."""
    result: dict[str, str] = {}
    result["os()"] = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(
        platform.system().lower(), platform.system().lower()
    )
    result["os_family()"] = "unix" if result["os()"] in ("linux", "macos") else result["os()"]
    result["arch()"] = platform.machine()
    result["num_cpus()"] = str(os.cpu_count() or 1)
    result["home_directory()"] = str(os.path.expanduser("~"))
    result["invocation_directory()"] = os.getcwd()
    result["cwd()"] = os.getcwd()
    return result


def parse_duration_seconds(duration: str) -> float:
    """Parse a duration like '30s', '5m', '1h', '2d', or bare seconds."""
    m = re.fullmatch(r"(\d+)\s*([smhd])?", duration.strip())
    if not m:
        raise ValueError(f"invalid duration: {duration!r} (expected e.g. '30s', '5m', '1h')")
    value = int(m.group(1))
    unit = m.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


# ---------------------------------------------------------------------------
# String functions (Justfile-compatible + extensions)
# ---------------------------------------------------------------------------


def _fn_uppercase(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("uppercase() takes exactly 1 argument")
    return args[0].upper()


def _fn_lowercase(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("lowercase() takes exactly 1 argument")
    return args[0].lower()


def _fn_trim(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("trim() takes exactly 1 argument")
    return args[0].strip()


def _fn_trim_start(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("trim_start() takes exactly 1 argument")
    return args[0].lstrip()


def _fn_trim_end(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("trim_end() takes exactly 1 argument")
    return args[0].rstrip()


def _fn_replace(*args: str) -> str:
    if len(args) != 3:
        raise ValueError("replace() takes exactly 3 arguments")
    return args[0].replace(args[1], args[2])


def _fn_replace_regex(*args: str) -> str:
    if len(args) != 3:
        raise ValueError("replace_regex() takes exactly 3 arguments")
    return re.sub(args[1], args[2], args[0])


def _fn_quote(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("quote() takes exactly 1 argument")
    return shlex.quote(args[0])


def _fn_join(*args: str) -> str:
    if len(args) < 2:
        raise ValueError("join() takes at least 2 arguments (separator, values...)")
    sep = args[0]
    return sep.join(args[1:])


def _fn_env_var(*args: str) -> str:
    if len(args) not in (1, 2):
        raise ValueError("env_var() takes 1 or 2 arguments")
    return os.environ.get(args[0], args[1] if len(args) == 2 else "")


def _fn_path_exists(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("path_exists() takes exactly 1 argument")
    return "true" if os.path.exists(args[0]) else "false"


# Path functions
def _fn_file_name(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("file_name() takes exactly 1 argument")
    return PurePosixPath(args[0]).name


def _fn_file_stem(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("file_stem() takes exactly 1 argument")
    return PurePosixPath(args[0]).stem


def _fn_parent_directory(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("parent_directory() takes exactly 1 argument")
    return str(PurePosixPath(args[0]).parent)


def _fn_extension(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("extension() takes exactly 1 argument")
    ext = PurePosixPath(args[0]).suffix
    return ext.lstrip(".")


def _fn_without_extension(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("without_extension() takes exactly 1 argument")
    p = PurePosixPath(args[0])
    return str(p.with_suffix(""))


# Boolean / utility functions
def _fn_contains(*args: str) -> str:
    if len(args) != 2:
        raise ValueError("contains() takes exactly 2 arguments")
    return "true" if args[1] in args[0] else "false"


def _fn_starts_with(*args: str) -> str:
    if len(args) != 2:
        raise ValueError("starts_with() takes exactly 2 arguments")
    return "true" if args[0].startswith(args[1]) else "false"


def _fn_ends_with(*args: str) -> str:
    if len(args) != 2:
        raise ValueError("ends_with() takes exactly 2 arguments")
    return "true" if args[0].endswith(args[1]) else "false"


def _fn_len(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("len() takes exactly 1 argument")
    return str(len(args[0]))


def _fn_substr(*args: str) -> str:
    if len(args) not in (2, 3):
        raise ValueError("substr() takes 2 or 3 arguments")
    s = args[0]
    start = int(args[1])
    if len(args) == 3:
        end = int(args[2])
        return s[start:end]
    return s[start:]


def _fn_match(*args: str) -> str:
    if len(args) != 2:
        raise ValueError("match() takes exactly 2 arguments")
    return "true" if re.search(args[1], args[0]) else "false"


# Version functions
def _parse_semver(v: str) -> tuple[int, int, int]:
    """Parse a semver string like '1.2.3' into (major, minor, patch)."""
    v = v.lstrip("v")
    parts = v.split(".")
    if len(parts) != 3:
        raise ValueError(f"invalid semver: {v!r} (expected X.Y.Z)")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _fn_version_major(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("version_major() takes exactly 1 argument")
    major, _, _ = _parse_semver(args[0])
    return str(major)


def _fn_version_minor(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("version_minor() takes exactly 1 argument")
    _, minor, _ = _parse_semver(args[0])
    return str(minor)


def _fn_version_patch(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("version_patch() takes exactly 1 argument")
    _, _, patch = _parse_semver(args[0])
    return str(patch)


def _fn_bump_major(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("bump_major() takes exactly 1 argument")
    major, _, _ = _parse_semver(args[0])
    return f"{major + 1}.0.0"


def _fn_bump_minor(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("bump_minor() takes exactly 1 argument")
    major, minor, _ = _parse_semver(args[0])
    return f"{major}.{minor + 1}.0"


def _fn_bump_patch(*args: str) -> str:
    if len(args) != 1:
        raise ValueError("bump_patch() takes exactly 1 argument")
    major, minor, patch = _parse_semver(args[0])
    return f"{major}.{minor}.{patch + 1}"


def _fn_changed_files(*args: str) -> str:
    """Return space-separated paths that changed against a ref."""
    if len(args) > 1:
        raise ValueError("changed_files() takes at most 1 argument")
    ref = args[0] if args else None
    return " ".join(gitinfo.changed_files(ref))


def _fn_changed(*args: str) -> str:
    """Return whether any changed path matches a glob.

    ``changed("src/**")`` compares against the default ref; ``changed("main",
    "src/**")`` compares against an explicit one.
    """
    if len(args) == 1:
        ref, pattern = None, args[0]
    elif len(args) == 2:
        ref, pattern = args[0], args[1]
    else:
        raise ValueError("changed() takes 1 or 2 arguments")
    return "true" if gitinfo.matches(gitinfo.changed_files(ref), pattern) else "false"


def _fn_git_branch(*args: str) -> str:
    if args:
        raise ValueError("git_branch() takes no arguments")
    return gitinfo.branch()


def _fn_git_sha(*args: str) -> str:
    if len(args) > 1:
        raise ValueError("git_sha() takes at most 1 argument")
    short = args[0].strip().lower() not in ("false", "0", "no") if args else True
    return gitinfo.sha(short=short)


# Registry of callable string functions
_STRING_FUNCTIONS: dict[str, Callable[..., str]] = {
    # String manipulation
    "uppercase": _fn_uppercase,
    "lowercase": _fn_lowercase,
    "trim": _fn_trim,
    "trim_start": _fn_trim_start,
    "trim_end": _fn_trim_end,
    "replace": _fn_replace,
    "replace_regex": _fn_replace_regex,
    "quote": _fn_quote,
    "join": _fn_join,
    "env_var": _fn_env_var,
    "path_exists": _fn_path_exists,
    # Path functions
    "file_name": _fn_file_name,
    "file_stem": _fn_file_stem,
    "parent_directory": _fn_parent_directory,
    "extension": _fn_extension,
    "without_extension": _fn_without_extension,
    # Boolean / utility
    "contains": _fn_contains,
    "starts_with": _fn_starts_with,
    "ends_with": _fn_ends_with,
    "len": _fn_len,
    "substr": _fn_substr,
    "match": _fn_match,
    # Version functions
    "version_major": _fn_version_major,
    "version_minor": _fn_version_minor,
    "version_patch": _fn_version_patch,
    "bump_major": _fn_bump_major,
    "bump_minor": _fn_bump_minor,
    "bump_patch": _fn_bump_patch,
    # Git-aware inputs
    "changed": _fn_changed,
    "changed_files": _fn_changed_files,
    "git_branch": _fn_git_branch,
    "git_sha": _fn_git_sha,
}


def _parse_function_args(raw: str) -> list[str]:
    """Parse comma-separated function arguments, respecting quoted strings."""
    args: list[str] = []
    current = ""
    depth = 0
    in_quote: str | None = None

    for ch in raw:
        if in_quote:
            if ch == in_quote:
                in_quote = None
            current += ch
        elif ch in ('"', "'"):
            in_quote = ch
            current += ch
        elif ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            args.append(current.strip())
            current = ""
        else:
            current += ch

    if current.strip():
        args.append(current.strip())
    return args


def _call_function(name: str, args: list[str], context: dict[str, str]) -> str | None:
    """Call a named string function with resolved arguments.

    Returns None if the function is not found.
    """
    fn = _STRING_FUNCTIONS.get(name)
    if fn is None:
        return None

    # Resolve each arg: could be a quoted string, a variable, or a nested function call
    resolved_args: list[str] = []
    for arg in args:
        arg = arg.strip()
        resolved_args.append(_resolve_function_arg(arg, context))

    return fn(*resolved_args)


def _resolve_function_arg(arg: str, context: dict[str, str]) -> str:
    """Resolve a single function argument: quoted string, variable, or nested function call."""
    arg = arg.strip()

    # Quoted string
    if (arg.startswith('"') and arg.endswith('"')) or (arg.startswith("'") and arg.endswith("'")):
        return arg[1:-1]

    # Nested function call: name(args...)
    paren = arg.find("(")
    if paren != -1 and arg.endswith(")"):
        fn_name = arg[:paren].strip()
        inner_args_raw = arg[paren + 1 : -1]
        inner_args = _parse_function_args(inner_args_raw)
        result = _call_function(fn_name, inner_args, context)
        if result is not None:
            return result

    # Variable lookup
    if arg in context:
        return context[arg]

    return arg


def _apply_parameter_expansion(var_value: str, operator: str, pattern: str) -> str:
    """Apply bash-style parameter expansion operators.

    Supported operators: #, ##, %, %%, /, //
    """
    return apply_parameter_expansion(var_value, operator, pattern)


def evaluate_condition(expr: str, context: dict[str, str]) -> bool:
    """Evaluate a condition expression for [when=...] clauses.

    Supports:
    - Variable comparisons: ``var == "value"``, ``var != "value"``, ``>``/``<``/``>=``/``<=``
    - Artifact access: ``task.success``, ``task.exit_code``
    - File existence: ``exists("path")``, ``!exists("path")``
    - Truthy variables: bare ``varname`` evaluates to True if non-empty and not "false"/"0"
    - Negation: ``!varname`` evaluates to True if empty or "false"/"0"
    - Backtick command: `` `command` `` evaluates to True if exit code is 0
    """
    expr = expr.strip()

    # Negated expression: !expr
    if expr.startswith("!"):
        inner = expr[1:].strip()
        return not evaluate_condition(inner, context)

    # File existence: exists("path")
    if expr.startswith("exists(") and expr.endswith(")"):
        path_expr = expr[7:-1].strip()
        path = _resolve_value(path_expr, context)
        return os.path.exists(path)

    # Backtick command: `cmd` — true if exit code 0
    if expr.startswith("`") and expr.endswith("`"):
        cmd = expr[1:-1]
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", cmd],
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    # Comparison operators: ==, !=, >=, <=, >, <
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        idx = expr.find(op)
        if idx > 0:
            lhs = expr[:idx].strip()
            rhs = expr[idx + len(op) :].strip()
            lhs_val = _resolve_value(lhs, context)
            rhs_val = _resolve_value(rhs, context)
            if op == "==":
                return lhs_val == rhs_val
            elif op == "!=":
                return lhs_val != rhs_val
            else:
                return _compare_values(lhs_val, rhs_val, op)

    # Bare variable: truthy check
    val = _resolve_value(expr, context)
    return val not in ("", "false", "0")


def condition_uses_shell(expr: str) -> bool:
    """Return whether evaluating a condition can invoke a shell command."""
    normalized = expr.strip()
    while normalized.startswith("!"):
        normalized = normalized[1:].strip()
    return normalized.startswith("`") and normalized.endswith("`")


def _evaluate_expression(expr: str, variables: dict[str, str]) -> str:
    """Evaluate a simple expression with if/else and string concatenation.

    Supports:
        if VAR == "value" { "then" } else { "otherwise" }
        if VAR != "value" { "then" } else { "otherwise" }
        "a" + "b" + variable
    """
    expr = expr.strip()

    # if/else expression
    if expr.startswith("if "):
        return _eval_if_else(expr, variables)

    # String concatenation with +
    if "+" in expr:
        return _eval_concat(expr, variables)

    # Bare variable or quoted string
    return _resolve_value(expr, variables)


def _resolve_value(token: str, variables: dict[str, str]) -> str:
    """Resolve a single value: quoted string, variable, or built-in function call."""
    token = token.strip()
    # Quoted string
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        return token[1:-1]
    # Variable lookup (including built-in function calls like os())
    if token in variables:
        return variables[token]
    return token


def _eval_concat(expr: str, variables: dict[str, str]) -> str:
    """Evaluate string concatenation: "a" + b + "c"."""
    parts = split_unquoted(expr, "+")
    return "".join(_resolve_value(p, variables) for p in parts)


def _compare_values(lhs: str, rhs: str, op: str) -> bool:
    """Compare two values using the given operator.

    Attempts semver comparison first, then numeric, then lexicographic.
    """
    # Try semver comparison
    try:
        lhs_ver = _parse_semver(lhs)
        rhs_ver = _parse_semver(rhs)
        if op == ">=":
            return lhs_ver >= rhs_ver
        elif op == "<=":
            return lhs_ver <= rhs_ver
        elif op == ">":
            return lhs_ver > rhs_ver
        elif op == "<":
            return lhs_ver < rhs_ver
    except (ValueError, IndexError):
        pass

    # Try numeric comparison
    try:
        lhs_num = float(lhs)
        rhs_num = float(rhs)
        if op == ">=":
            return lhs_num >= rhs_num
        elif op == "<=":
            return lhs_num <= rhs_num
        elif op == ">":
            return lhs_num > rhs_num
        elif op == "<":
            return lhs_num < rhs_num
    except ValueError:
        pass

    # Fall back to lexicographic
    if op == ">=":
        return lhs >= rhs
    elif op == "<=":
        return lhs <= rhs
    elif op == ">":
        return lhs > rhs
    elif op == "<":
        return lhs < rhs
    return False


def _eval_if_else(expr: str, variables: dict[str, str]) -> str:
    """Evaluate: if VAR == "val" { "then" } else { "otherwise" }."""
    # Remove leading "if "
    rest = expr[3:].strip()

    # Find the operator (check multi-char operators first)
    op = None
    op_pos = -1
    for candidate in ("==", "!=", ">=", "<=", ">", "<"):
        pos = rest.find(candidate)
        if pos != -1 and (op_pos == -1 or pos < op_pos):
            op = candidate
            op_pos = pos

    if op is None or op_pos == -1:
        return ""

    lhs = rest[:op_pos].strip()
    after_op = rest[op_pos + len(op) :].strip()

    # Parse: "val" { "then_branch" } else { "else_branch" }
    # Find the first { ... }
    first_brace = after_op.find("{")
    if first_brace == -1:
        return ""
    rhs = after_op[:first_brace].strip()

    # Find matching close brace
    depth = 0
    first_close = -1
    for i in range(first_brace, len(after_op)):
        if after_op[i] == "{":
            depth += 1
        elif after_op[i] == "}":
            depth -= 1
            if depth == 0:
                first_close = i
                break

    if first_close == -1:
        return ""

    then_body = after_op[first_brace + 1 : first_close].strip()

    # Find else { ... }
    else_part = after_op[first_close + 1 :].strip()
    else_body = ""
    if else_part.startswith("else"):
        else_rest = else_part[4:].strip()
        eb_start = else_rest.find("{")
        eb_end = else_rest.rfind("}")
        if eb_start != -1 and eb_end > eb_start:
            else_body = else_rest[eb_start + 1 : eb_end].strip()

    lhs_val = _resolve_value(lhs, variables)
    rhs_val = _resolve_value(rhs, variables)

    if op == "==":
        condition = lhs_val == rhs_val
    elif op == "!=":
        condition = lhs_val != rhs_val
    elif op in (">=", "<=", ">", "<"):
        condition = _compare_values(lhs_val, rhs_val, op)
    else:
        condition = False

    result_expr = then_body if condition else else_body
    return _resolve_value(result_expr, variables)


@dataclass
class Promptfile:
    """The parsed representation of an entire Promptfile."""

    variables: dict[str, str] = field(default_factory=dict)
    included_variables: set[str] = field(default_factory=set, repr=False)
    exported_vars: set[str] = field(default_factory=set)  # vars with 'export' prefix
    tasks: dict[str, Task] = field(default_factory=dict)
    functions: dict[str, Function] = field(default_factory=dict)
    task_order: list[str] = field(default_factory=list)
    llm_providers: dict[str, LLMProvider] = field(default_factory=dict)
    default_llm: str | None = None
    host_groups: dict[str, HostGroup] = field(default_factory=dict)
    agents: dict[str, Agent] = field(default_factory=dict)
    guidance: str | None = None  # global default prompt prepended to all tasks
    settings: Settings = field(default_factory=Settings)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> target
    mcp_servers: dict[str, MCPServer] = field(default_factory=dict)

    @property
    def default_task(self) -> str | None:
        """Return the default task: explicit ``set default`` or first defined task."""
        if self.settings.default:
            return self.settings.default
        return self.task_order[0] if self.task_order else None

    def resolve_alias(self, name: str) -> str:
        """Resolve an alias to its target task name."""
        return self.aliases.get(name, name)

    def get_llm_for_task(self, task_name: str) -> LLMProvider | None:
        """Return the LLM provider for a task (per-task override > global default)."""
        task = self.tasks[task_name]
        provider_name = task.options.llm or self.default_llm
        if provider_name and provider_name in self.llm_providers:
            return self.llm_providers[provider_name]
        return None

    def get_agent_for_task(self, task_name: str) -> Agent | None:
        """Return the agent for a task, if [agent=name] is set."""
        task = self.tasks[task_name]
        agent_name = task.options.agent
        if agent_name and agent_name in self.agents:
            return self.agents[agent_name]
        return None

    def get_hosts_for_task(self, task_name: str) -> HostGroup | None:
        """Return the host group for a task, if [on=group] is set."""
        task = self.tasks[task_name]
        group_name = task.options.on
        if group_name and group_name in self.host_groups:
            return self.host_groups[group_name]
        return None

    def get_exported_env(self) -> dict[str, str]:
        """Return variables that should be exported to the environment.

        If ``set export`` is true, all variables are exported.
        Otherwise only variables declared with ``export`` are exported.
        """
        if self.settings.export:
            return dict(self.variables)
        return {k: v for k, v in self.variables.items() if k in self.exported_vars}

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------

    def _expand_uses(self, text: str, task_name: str | None = None) -> str:
        """Replace ``@use fn_name`` lines with the function body."""
        lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("@use "):
                fn_name = stripped[5:].strip()
                if (
                    fn_name not in self.functions
                    and task_name
                    and self.tasks[task_name].function_namespace
                ):
                    fn_name = f"{self.tasks[task_name].function_namespace}::{fn_name}"
                if fn_name not in self.functions:
                    raise KeyError(f"unknown function: {fn_name!r}")
                lines.append(self.functions[fn_name].body)
            else:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _resolve_env_vars(text: str) -> str:
        """Resolve ``${VAR:-default}`` and ``${VAR}`` from the environment."""
        return resolve_env_vars(text)

    def _secret_backend_for_task(self, task: Task) -> str:
        """Return the secrets backend for a task."""
        from .secrets import secret_backend_for_task

        return secret_backend_for_task(self.settings, task)

    def _resolve_secret(
        self,
        secret_ref: str,
        task: Task,
        *,
        promptfile_path: str | None = None,
    ) -> str:
        """Resolve a single ``{{#secret:...}}`` reference."""
        return resolve_secret(
            self.settings,
            secret_ref,
            task,
            promptfile_path=promptfile_path,
        )

    def _audit_secret_resolution(self, secret_ref: str, task: Task) -> None:
        """Log secret resolution metadata without exposing the resolved value."""
        from .secrets import audit_secret_resolution

        audit_secret_resolution(self.settings, secret_ref, task)

    def _resolve_secrets(
        self,
        text: str,
        task: Task,
        *,
        promptfile_path: str | None = None,
        mask_only: bool = False,
        secret_callback: Callable[[str], None] | None = None,
    ) -> str:
        """Resolve ``{{#secret:NAME}}`` placeholders in text."""
        return resolve_secrets(
            text,
            self.settings,
            task,
            promptfile_path=promptfile_path,
            mask_only=mask_only,
            secret_callback=secret_callback,
        )

    def task_secret_sources(
        self,
        task_name: str,
        args: dict[str, str] | None = None,
    ) -> list[str]:
        """Return task text after non-secret expansion, without resolving secrets."""
        task = self.tasks[task_name]
        context = self._build_context(task_name, args)
        sources: list[str] = []
        for step in task.steps:
            content = step.content
            sources.append(content)
            if step.kind == "prompt":
                content = self._expand_uses(content, task_name)
                sources.append(content)
                content = self._interpolate(content, context)
                sources.append(content)
                content = self._resolve_env_vars(content)
            else:
                content = self._interpolate(content, context)
            sources.append(content)
        for raw_value in task.options.env.values():
            sources.append(raw_value)
            value = self._interpolate(raw_value, context)
            sources.append(self._resolve_env_vars(value))
        agent = self.get_agent_for_task(task_name)
        if agent:
            sources.append(agent.instructions)
        if task.guidance:
            sources.append(task.guidance)
        if self.guidance:
            sources.append(self.guidance)
        return sources

    def task_secret_references(
        self,
        task_name: str,
        args: dict[str, str] | None = None,
    ) -> set[str]:
        """Return explicit or secret-like environment references reachable by a task."""
        refs: set[str] = set()
        sources = self.task_secret_sources(task_name, args)
        for source in sources:
            refs.update(
                match.strip()
                for match in re.findall(r"\{\{#secret:(.+?)\}\}", source)
                if match.strip()
            )
            for expression in re.findall(r"\{\{(?!#secret:)(.+?)\}\}", source):
                refs.update(
                    identifier
                    for identifier in re.findall(
                        r"[A-Za-z_][A-Za-z0-9_]*",
                        expression,
                    )
                    if is_secret_name(identifier)
                )
            env_names = re.findall(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}"
                r"|(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)"
                r"|env_var\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']",
                source,
            )
            refs.update(name for groups in env_names for name in groups if name)

        task = self.tasks[task_name]
        refs.update(name for name in task.options.env if is_secret_name(name))
        effective_args = dict(args or {})
        for argument in task.arguments:
            if argument.name not in effective_args and argument.default is not None:
                effective_args[argument.name] = argument.default
        refs.update(name for name in effective_args if is_secret_name(name))

        known_values = {
            value
            for name, value in {
                **dict(os.environ),
                **self.variables,
                **task.local_variables,
                **effective_args,
            }.items()
            if is_secret_name(name) and value
        }
        if known_values:
            for source in sources:
                if any(value in source for value in known_values):
                    refs.add("interpolated-secret-value")
                    break
        return refs

    def task_references_secret(
        self,
        task_name: str,
        args: dict[str, str] | None = None,
    ) -> bool:
        """Return whether resolving a task can reach a secret placeholder."""
        return bool(self.task_secret_references(task_name, args))

    def _build_context(
        self,
        task_name: str,
        args: dict[str, str] | None = None,
        *,
        promptfile_path: str | None = None,
    ) -> dict[str, str]:
        """Build the variable context for a task."""
        # Start with built-in functions
        context = _builtin_functions()
        # Layer on user variables
        context.update(self.variables)
        task = self.tasks[task_name]
        # Module-scoped variables override the importing Promptfile's globals.
        context.update(task.local_variables)
        # Layer on task args
        if args:
            context.update(args)
        # Fill in defaults for missing args
        if task.arguments:
            for arg in task.arguments:
                if arg.name not in context and arg.default is not None:
                    context[arg.name] = arg.default
        # Runtime variables
        context["makethlm_task"] = task_name
        if promptfile_path:
            context["makethlm_file"] = promptfile_path
            context["makethlm_dir"] = os.path.dirname(os.path.abspath(promptfile_path))
        else:
            context["makethlm_file"] = ""
            context["makethlm_dir"] = os.getcwd()
        return context

    def _interpolate(self, text: str, context: dict[str, str]) -> str:
        """Replace {{name}} and evaluate {{ expressions }}.

        Supports:
        - Simple variable lookup: {{name}}
        - Built-in function calls: {{os()}}
        - String function calls: {{uppercase(name)}}
        - If/else expressions: {{if x == "y" { "a" } else { "b" }}}
        - String concatenation: {{"a" + "b"}}
        - Bash-style parameter expansion: {{name##pattern}}, {{name/old/new}}
        """
        return interpolate_text(
            text,
            context,
            string_functions=_STRING_FUNCTIONS,
            parse_function_args=_parse_function_args,
            call_function=_call_function,
            evaluate_expression=_evaluate_expression,
        )

    @staticmethod
    def _try_parameter_expansion(expr: str, context: dict[str, str]) -> str | None:
        """Try to parse bash-style parameter expansion from an expression.

        Returns None if no expansion pattern is matched.
        Supports: ##, #, %%, %, //, /
        """
        return try_parameter_expansion(expr, context)

    def resolve_prompt(
        self,
        task_name: str,
        args: dict[str, str] | None = None,
        *,
        promptfile_path: str | None = None,
        mask_secrets: bool = False,
        secret_callback: Callable[[str], None] | None = None,
    ) -> str:
        """Return the prompt for a task with @use, {{variables}}, and env vars resolved."""
        task = self.tasks[task_name]
        prompt = task.prompt

        if not self.settings.allow_secrets_in_prompts and self.task_references_secret(
            task_name, args
        ):
            raise SecretError(
                f"task {task_name!r} references a secret in an LLM prompt "
                "or other sensitive input; "
                "enable it with set allow-secrets-in-prompts true"
            )
        prompt = self._expand_uses(prompt, task_name)
        context = self._build_context(task_name, args, promptfile_path=promptfile_path)
        prompt = self._interpolate(prompt, context)
        prompt = self._resolve_env_vars(prompt)
        prompt = self._resolve_secrets(
            prompt,
            task,
            promptfile_path=promptfile_path,
            mask_only=mask_secrets,
            secret_callback=secret_callback,
        )

        return prompt

    def resolve_steps(
        self,
        task_name: str,
        args: dict[str, str] | None = None,
        *,
        promptfile_path: str | None = None,
        mask_secrets: bool = False,
        secret_callback: Callable[[str], None] | None = None,
    ) -> list[TaskStep]:
        """Return fully-resolved steps (shell commands get {{var}} interpolation only)."""
        task = self.tasks[task_name]
        context = self._build_context(task_name, args, promptfile_path=promptfile_path)
        agent = self.get_agent_for_task(task_name)

        if (
            not self.settings.allow_secrets_in_prompts
            and any(step.kind == "prompt" for step in task.steps)
            and self.task_references_secret(task_name, args)
        ):
            raise SecretError(
                f"task {task_name!r} references a secret in an LLM prompt "
                "or other sensitive input; "
                "enable it with set allow-secrets-in-prompts true"
            )
        resolved: list[TaskStep] = []
        for step in task.steps:
            content = step.content

            if step.kind == "prompt":
                content = self._expand_uses(content, task_name)
                content = self._interpolate(content, context)
                content = self._resolve_env_vars(content)
                # Prepend agent instructions and guidance to every prompt step
                # Order: global guidance -> module guidance -> agent -> task.
                if agent:
                    content = agent.instructions + "\n\n" + content
                if task.guidance:
                    content = task.guidance + "\n\n" + content
                if self.guidance:
                    content = self.guidance + "\n\n" + content
            else:
                content = self._interpolate(content, context)
            content = self._resolve_secrets(
                content,
                task,
                promptfile_path=promptfile_path,
                mask_only=mask_secrets,
                secret_callback=secret_callback,
            )

            resolved.append(
                TaskStep(
                    kind=step.kind,
                    content=content,
                    silent=step.silent,
                    ignore_error=step.ignore_error,
                    quiet=step.quiet,
                    capture=step.capture,
                    pipe_output=step.pipe_output,
                    llm=step.llm,
                    script=step.script,
                )
            )

        return resolved
