"""Data models for the Promptfile AST."""

from __future__ import annotations

import os
import re
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
class TaskOptions:
    """Optional metadata attached to a task via [key=value, ...] syntax."""

    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    llm: str | None = None  # per-task LLM provider override
    on: str | None = None   # host group to run on (Ansible-like)

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
        )


@dataclass
class TaskStep:
    """A single step within a task body.

    kind="shell"  — a line starting with !, executed as a subprocess.
    kind="prompt" — natural-language text sent to an LLM.
    """

    kind: Literal["shell", "prompt"]
    content: str
    silent: bool = False        # @silent — suppress stdout
    ignore_error: bool = False  # @ignore — continue on non-zero exit


@dataclass
class TaskArgument:
    """A positional argument for a task: task deploy(target, port="8080")."""

    name: str
    default: str | None = None


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


@dataclass
class Promptfile:
    """The parsed representation of an entire Promptfile."""

    variables: dict[str, str] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    functions: dict[str, Function] = field(default_factory=dict)
    task_order: list[str] = field(default_factory=list)
    llm_providers: dict[str, LLMProvider] = field(default_factory=dict)
    default_llm: str | None = None  # name of the default LLM provider
    host_groups: dict[str, HostGroup] = field(default_factory=dict)

    @property
    def default_task(self) -> str | None:
        """The first defined task is the default, like Make."""
        return self.task_order[0] if self.task_order else None

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
        """Resolve ``${VAR:-default}`` and ``$VAR`` from the environment."""
        def _with_default(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2))

        text = re.sub(r"\$\{(\w+):-([^}]*)\}", _with_default, text)

        def _braced(m: re.Match) -> str:
            return os.environ.get(m.group(1), "")

        text = re.sub(r"\$\{(\w+)\}", _braced, text)
        return text

    def resolve_prompt(self, task_name: str, args: dict[str, str] | None = None) -> str:
        """Return the prompt for a task with @use, {{variables}}, and env vars resolved."""
        task = self.tasks[task_name]
        prompt = task.prompt

        prompt = self._expand_uses(prompt)

        context = dict(self.variables)
        if args:
            context.update(args)
        if task.arguments:
            for arg in task.arguments:
                if arg.name not in context and arg.default is not None:
                    context[arg.name] = arg.default

        for key, value in context.items():
            prompt = prompt.replace("{{" + key + "}}", value)

        prompt = self._resolve_env_vars(prompt)

        return prompt

    def resolve_steps(self, task_name: str, args: dict[str, str] | None = None) -> list[TaskStep]:
        """Return fully-resolved steps (shell commands get {{var}} interpolation only)."""
        task = self.tasks[task_name]

        context = dict(self.variables)
        if args:
            context.update(args)
        if task.arguments:
            for arg in task.arguments:
                if arg.name not in context and arg.default is not None:
                    context[arg.name] = arg.default

        resolved: list[TaskStep] = []
        for step in task.steps:
            content = step.content

            if step.kind == "prompt":
                content = self._expand_uses(content)
                for key, value in context.items():
                    content = content.replace("{{" + key + "}}", value)
                content = self._resolve_env_vars(content)
            else:
                for key, value in context.items():
                    content = content.replace("{{" + key + "}}", value)

            resolved.append(TaskStep(
                kind=step.kind,
                content=content,
                silent=step.silent,
                ignore_error=step.ignore_error,
            ))

        return resolved
