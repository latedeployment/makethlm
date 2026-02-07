"""Data models for the Promptfile AST."""

from __future__ import annotations

import os
import re
import platform
import subprocess
from dataclasses import dataclass, field
from typing import Literal


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


@dataclass
class Settings:
    """Global configuration from ``set`` directives (Justfile-compatible)."""

    dotenv_load: bool = False
    dotenv_path: str | None = None      # custom .env path
    dotenv_required: bool = False        # error if .env missing
    shell: str | None = None             # shell executable (default: sh)
    working_dir: str | None = None       # global working directory
    export: bool = False                 # export all variables to env
    positional_arguments: bool = False   # pass task args as $1, $2...
    fallback: bool = False               # search parent dirs for Promptfile
    ignore_comments: bool = False        # strip # comments from shell cmds
    tempdir: str | None = None           # temp directory for recipes
    quiet: bool = False                  # suppress command echoing globally
    allow_duplicate_tasks: bool = False  # allow redefining tasks
    allow_duplicate_variables: bool = False


@dataclass
class TaskOptions:
    """Optional metadata attached to a task via [key=value, ...] syntax."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    llm: str | None = None         # per-task LLM provider override
    on: str | None = None          # host group to run on (Ansible-like)
    private: bool = False          # hide from --list
    group: str | None = None       # group name for --list display
    doc: str | None = None         # one-line description for --list
    confirm: str | bool = False    # True or custom message
    os_filter: str | None = None   # "linux", "macos", "windows", "unix"
    working_dir: str | None = None # per-task working directory
    no_cd: bool = False            # don't change to working dir
    no_exit_message: bool = False  # suppress error message on failure
    no_quiet: bool = False         # override global quiet for this task
    positional_arguments: bool | None = None  # per-task override

    def merge(self, overrides: "TaskOptions") -> "TaskOptions":
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
            on=overrides.on if overrides.on is not None else self.on,
            private=overrides.private or self.private,
            group=overrides.group if overrides.group is not None else self.group,
            doc=overrides.doc if overrides.doc is not None else self.doc,
            confirm=overrides.confirm if overrides.confirm else self.confirm,
            os_filter=overrides.os_filter if overrides.os_filter is not None else self.os_filter,
            working_dir=overrides.working_dir if overrides.working_dir is not None else self.working_dir,
            no_cd=overrides.no_cd or self.no_cd,
            no_exit_message=overrides.no_exit_message or self.no_exit_message,
            no_quiet=overrides.no_quiet or self.no_quiet,
            positional_arguments=overrides.positional_arguments if overrides.positional_arguments is not None else self.positional_arguments,
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
    """

    kind: Literal["shell", "prompt"]
    content: str
    silent: bool = False        # @silent -- suppress stdout
    ignore_error: bool = False  # @ignore -- continue on non-zero exit
    quiet: bool = False         # @ prefix -- suppress command echoing


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
class HostGroup:
    """A named group of hosts for Ansible-like remote execution.

    Defined with ``hosts <name>:`` followed by indented hostnames.
    """

    name: str
    hosts: list[str] = field(default_factory=list)
    user: str | None = None     # SSH user override
    port: int | None = None     # SSH port override
    line_number: int = 0


@dataclass
class Task:
    """A single task definition from a Promptfile."""

    name: str
    steps: list[TaskStep] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    options: TaskOptions = field(default_factory=TaskOptions)
    arguments: list[TaskArgument] = field(default_factory=list)
    docker: DockerConfig | None = None
    line_number: int = 0

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
    return result


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
    if (token.startswith('"') and token.endswith('"')) or \
       (token.startswith("'") and token.endswith("'")):
        return token[1:-1]
    # Variable lookup (including built-in function calls like os())
    if token in variables:
        return variables[token]
    return token


def _eval_concat(expr: str, variables: dict[str, str]) -> str:
    """Evaluate string concatenation: "a" + b + "c"."""
    parts = expr.split("+")
    return "".join(_resolve_value(p, variables) for p in parts)


def _eval_if_else(expr: str, variables: dict[str, str]) -> str:
    """Evaluate: if VAR == "val" { "then" } else { "otherwise" }."""
    # Remove leading "if "
    rest = expr[3:].strip()

    # Find the operator
    op = None
    op_pos = -1
    for candidate in ("==", "!="):
        pos = rest.find(candidate)
        if pos != -1 and (op_pos == -1 or pos < op_pos):
            op = candidate
            op_pos = pos

    if op is None or op_pos == -1:
        return ""

    lhs = rest[:op_pos].strip()
    after_op = rest[op_pos + len(op):].strip()

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

    then_body = after_op[first_brace + 1:first_close].strip()

    # Find else { ... }
    else_part = after_op[first_close + 1:].strip()
    else_body = ""
    if else_part.startswith("else"):
        else_rest = else_part[4:].strip()
        eb_start = else_rest.find("{")
        eb_end = else_rest.rfind("}")
        if eb_start != -1 and eb_end > eb_start:
            else_body = else_rest[eb_start + 1:eb_end].strip()

    lhs_val = _resolve_value(lhs, variables)
    rhs_val = _resolve_value(rhs, variables)

    if op == "==":
        condition = lhs_val == rhs_val
    else:  # !=
        condition = lhs_val != rhs_val

    result_expr = then_body if condition else else_body
    return _resolve_value(result_expr, variables)


@dataclass
class Promptfile:
    """The parsed representation of an entire Promptfile."""

    variables: dict[str, str] = field(default_factory=dict)
    exported_vars: set[str] = field(default_factory=set)  # vars with 'export' prefix
    tasks: dict[str, Task] = field(default_factory=dict)
    functions: dict[str, Function] = field(default_factory=dict)
    task_order: list[str] = field(default_factory=list)
    llm_providers: dict[str, LLMProvider] = field(default_factory=dict)
    default_llm: str | None = None
    host_groups: dict[str, HostGroup] = field(default_factory=dict)
    settings: Settings = field(default_factory=Settings)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> target

    @property
    def default_task(self) -> str | None:
        """The first defined task is the default, like Make."""
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

    def _expand_uses(self, text: str) -> str:
        """Replace ``@use fn_name`` lines with the function body."""
        lines: list[str] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("@use "):
                fn_name = stripped[5:].strip()
                if fn_name not in self.functions:
                    raise KeyError(f"unknown function: {fn_name!r}")
                lines.append(self.functions[fn_name].body)
            else:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _resolve_env_vars(text: str) -> str:
        """Resolve ``${VAR:-default}`` and ``${VAR}`` from the environment."""
        def _with_default(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2))

        text = re.sub(r"\$\{(\w+):-([^}]*)\}", _with_default, text)

        def _braced(m: re.Match) -> str:
            return os.environ.get(m.group(1), "")

        text = re.sub(r"\$\{(\w+)\}", _braced, text)
        return text

    def _build_context(self, task_name: str, args: dict[str, str] | None = None) -> dict[str, str]:
        """Build the variable context for a task."""
        # Start with built-in functions
        context = _builtin_functions()
        # Layer on user variables
        context.update(self.variables)
        # Layer on task args
        if args:
            context.update(args)
        # Fill in defaults for missing args
        task = self.tasks[task_name]
        if task.arguments:
            for arg in task.arguments:
                if arg.name not in context and arg.default is not None:
                    context[arg.name] = arg.default
        return context

    def _interpolate(self, text: str, context: dict[str, str]) -> str:
        """Replace {{name}} and evaluate {{ expressions }}."""
        def _replace_match(m: re.Match) -> str:
            inner = m.group(1).strip()
            # Simple variable lookup first
            if inner in context:
                return context[inner]
            # Try expression evaluation (if/else, concat)
            if inner.startswith("if ") or "+" in inner or inner.endswith("()"):
                return _evaluate_expression(inner, context)
            return m.group(0)  # leave unchanged

        return re.sub(r"\{\{(.+?)\}\}", _replace_match, text)

    def resolve_prompt(self, task_name: str, args: dict[str, str] | None = None) -> str:
        """Return the prompt for a task with @use, {{variables}}, and env vars resolved."""
        task = self.tasks[task_name]
        prompt = task.prompt

        prompt = self._expand_uses(prompt)
        context = self._build_context(task_name, args)
        prompt = self._interpolate(prompt, context)
        prompt = self._resolve_env_vars(prompt)

        return prompt

    def resolve_steps(self, task_name: str, args: dict[str, str] | None = None) -> list[TaskStep]:
        """Return fully-resolved steps (shell commands get {{var}} interpolation only)."""
        task = self.tasks[task_name]
        context = self._build_context(task_name, args)

        resolved: list[TaskStep] = []
        for step in task.steps:
            content = step.content

            if step.kind == "prompt":
                content = self._expand_uses(content)
                content = self._interpolate(content, context)
                content = self._resolve_env_vars(content)
            else:
                content = self._interpolate(content, context)

            resolved.append(TaskStep(
                kind=step.kind,
                content=content,
                silent=step.silent,
                ignore_error=step.ignore_error,
                quiet=step.quiet,
            ))

        return resolved
