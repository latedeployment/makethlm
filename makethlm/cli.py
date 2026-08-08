"""CLI entry point for makethlm."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cost import parse_cost
from .dispatcher import (
    ClaudeDispatcher,
    CodexDispatcher,
    Dispatcher,
    DryRunDispatcher,
    OllamaDispatcher,
    OpenAIDispatcher,
    OpenCodeDispatcher,
    ShellDispatcher,
)
from .formatter import format_text
from .gitinfo import SINCE_ENV_VAR
from .history import get_run, list_runs, record_run
from .models import (
    Promptfile,
    SecretError,
    _builtin_functions,
    _evaluate_expression,
    condition_uses_shell,
)
from .parser import ParseError, parse
from .runner import (
    CycleError,
    Runner,
    RunResult,
    StepResult,
    TaskResult,
    _dispatcher_for_provider,
    topological_sort,
)
from .secrets import (
    is_secret_name,
    redact_named_values,
    redact_text,
    secret_values_from_mapping,
)
from .staleness import digest_sources, up_to_date_reason

# Discovery order within a directory. The first four names came first and stay
# first so existing projects keep resolving the same file; the rest follow the
# hidden/all-caps conventions that make and just users expect.
PROMPTFILE_NAMES = [
    "Promptfile",
    "promptfile",
    "Promptfile.pf",
    "promptfile.pf",
    ".promptfile",
    ".Promptfile",
    ".promptfile.pf",
    ".Promptfile.pf",
    "PROMPTFILE",
    "PROMPTFILE.pf",
]


_KNOWN_NATIVE_PROVIDERS = {"claude", "codex", "openai", "ollama", "opencode"}
_SECRETS_BACKEND_TOOLS = {
    "infisical": "infisical",
    "1password": "op",
    "sops": "sops",
}
_SANDBOX_TOOLS = {
    "docker": "docker",
    "systemd": "systemd-run",
    "bwrap": "bwrap",
}


def find_promptfile(directory: Path | None = None) -> Path | None:
    """Locate a Promptfile.

    Searches each known name in the current directory and its parents, then a
    global Promptfile under the XDG config directory.
    """
    d = directory or Path.cwd()
    for search_dir in (d, *d.parents):
        for name in PROMPTFILE_NAMES:
            candidate = search_dir / name
            if candidate.is_file():
                return candidate
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    for name in PROMPTFILE_NAMES:
        candidate = config_home / "makethlm" / name
        if candidate.is_file():
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="makethlm",
        description="A task runner where tasks are LLM prompts.",
    )
    ap.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Task to run (default: first task in file)",
    )
    ap.add_argument(
        "task_args",
        nargs="*",
        default=[],
        help="Positional arguments for the task",
    )
    ap.add_argument(
        "-f",
        "--file",
        type=Path,
        default=None,
        help="Path to Promptfile (default: auto-detect)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts/commands that would be sent without executing",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Emit machine-readable JSON output",
    )
    ap.add_argument(
        "--parallel",
        action="store_true",
        help="Run independent dependency tasks in parallel",
    )
    ap.add_argument(
        "--always-make",
        "-B",
        action="store_true",
        dest="always_make",
        help="Run tasks even when sources are unchanged or results are cached",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Maximum number of parallel task workers",
    )
    ap.add_argument(
        "--since",
        metavar="REF",
        default=None,
        help="Git ref that changed()/changed_files() compare against (default: HEAD)",
    )
    ap.add_argument(
        "--watch",
        action="store_true",
        help="Re-run the task whenever a watched source file changes",
    )
    ap.add_argument(
        "--watch-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="Polling interval for --watch (default: 1.0)",
    )
    ap.add_argument(
        "--max-cost",
        metavar="USD",
        dest="max_cost",
        default=None,
        help="Stop the run once LLM spend reaches this many US dollars",
    )
    ap.add_argument(
        "--log-llm",
        metavar="PATH",
        dest="log_llm",
        default=None,
        help="Append every LLM call to PATH as JSONL for live debugging",
    )
    ap.add_argument(
        "--fixtures",
        metavar="DIR",
        default=None,
        help="Serve LLM responses from recorded fixtures in DIR",
    )
    ap.add_argument(
        "--record-fixtures",
        action="store_true",
        dest="record_fixtures",
        help="Call providers normally and record their responses into --fixtures DIR",
    )
    ap.add_argument(
        "--list",
        "-l",
        action="store_true",
        dest="list_tasks",
        help="List available tasks and exit",
    )
    ap.add_argument(
        "--summary",
        "-s",
        action="store_true",
        help="List task names only (compact, one per line)",
    )
    ap.add_argument(
        "--dump",
        action="store_true",
        help="Dump the parsed Promptfile (variables, tasks, etc.)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Validate Promptfile references, tools, and risky capabilities",
    )
    ap.add_argument(
        "--capabilities",
        action="store_true",
        help="Explain the transitive execution capabilities required by a task",
    )
    ap.add_argument(
        "--plan",
        action="store_true",
        help="Preview execution order, steps, variables, providers, and hosts",
    )
    ap.add_argument(
        "--graph",
        action="store_true",
        help="Print the task dependency graph and exit",
    )
    ap.add_argument(
        "--graph-format",
        choices=("mermaid", "dot"),
        default="mermaid",
        help="Graph output format (default: mermaid)",
    )
    ap.add_argument(
        "--history",
        nargs="?",
        const="20",
        default=None,
        help="Show recent run history and exit (optional limit)",
    )
    ap.add_argument(
        "--no-history",
        action="store_true",
        help="Do not record this run in local history",
    )
    ap.add_argument(
        "--evaluate",
        metavar="EXPR",
        default=None,
        help="Evaluate an expression and print the result",
    )
    ap.add_argument(
        "--model",
        "-m",
        default=None,
        help="Default LLM model to use",
    )
    ap.add_argument(
        "--shell",
        default=None,
        help="LLM CLI argv template, e.g. 'openai chat -p \"{prompt}\"'",
    )
    ap.add_argument(
        "--codex",
        action="store_true",
        help="Use the Codex CLI as the default LLM dispatcher",
    )
    ap.add_argument(
        "--openai",
        action="store_true",
        help="Use the native OpenAI API dispatcher as the default LLM dispatcher",
    )
    ap.add_argument(
        "--ollama",
        action="store_true",
        help="Use the native Ollama HTTP dispatcher as the default LLM dispatcher",
    )
    ap.add_argument(
        "--opencode",
        action="store_true",
        help="Use the opencode CLI as the default LLM dispatcher",
    )
    ap.add_argument(
        "--safe",
        action="store_true",
        help="Enable restrictive safety checks before execution",
    )
    ap.add_argument(
        "--allow-backticks",
        action="store_true",
        help="Allow Promptfile backtick commands in safe or inspection modes",
    )
    ap.add_argument(
        "--allow-shell",
        action="store_true",
        help="Allow local shell steps in safe mode",
    )
    ap.add_argument(
        "--allow-ssh",
        action="store_true",
        help="Allow SSH shell steps in safe mode",
    )
    ap.add_argument(
        "--allow-docker",
        action="store_true",
        help="Allow docker blocks in safe mode",
    )
    ap.add_argument(
        "--allow-llm",
        action="store_true",
        help="Allow LLM prompt execution in safe mode",
    )
    ap.add_argument(
        "--allow-secrets",
        action="store_true",
        help="Allow tasks to read or interpolate secrets in safe mode",
    )
    ap.add_argument(
        "--allow-mcp",
        action="store_true",
        help="Allow tasks to attach MCP servers in safe mode",
    )
    ap.add_argument(
        "--allow-webhook",
        action="store_true",
        help="Allow webhook delivery in safe mode",
    )
    ap.add_argument(
        "--var",
        "-V",
        action="append",
        default=[],
        help="Override a variable: -V name=value",
    )
    ap.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress command echoing",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output (show all step details)",
    )
    return ap


def _build_dispatcher(args: argparse.Namespace, *, honor_dry_run: bool = True) -> Dispatcher:
    """Build the CLI-selected fallback dispatcher."""
    if honor_dry_run and args.dry_run:
        return DryRunDispatcher()
    if args.shell:
        return ShellDispatcher(args.shell)
    if args.codex:
        return CodexDispatcher(model=args.model)
    if args.openai:
        return OpenAIDispatcher(model=args.model)
    if args.ollama:
        return OllamaDispatcher(model=args.model)
    if args.opencode:
        return OpenCodeDispatcher(model=args.model)
    return ClaudeDispatcher(model=args.model)


def _task_dispatchers(
    pf: Promptfile,
    task_name: str,
    fallback_dispatcher: Dispatcher,
) -> list[Dispatcher]:
    """Return the primary and fallback dispatchers reachable by a task."""
    dispatchers: list[Dispatcher] = []
    provider_name = _provider_for_check(pf, task_name)
    provider = pf.llm_providers.get(provider_name) if provider_name else None
    dispatchers.append(_dispatcher_for_provider(provider) if provider else fallback_dispatcher)
    task = pf.tasks[task_name]
    for fallback_name in task.options.fallback_llms:
        fallback = pf.llm_providers.get(fallback_name)
        if fallback:
            dispatchers.append(_dispatcher_for_provider(fallback))
    return dispatchers


def _task_uses_local_execution_provider(
    pf: Promptfile,
    task_name: str,
    fallback_dispatcher: Dispatcher,
) -> bool:
    """Return whether a task can reach an LLM dispatcher that executes locally."""
    # opencode runs an agent with --auto, so it can execute locally like the
    # other CLI-backed providers.
    local_dispatchers = (
        ClaudeDispatcher,
        CodexDispatcher,
        OpenCodeDispatcher,
        ShellDispatcher,
    )
    return any(
        isinstance(dispatcher, local_dispatchers)
        for dispatcher in _task_dispatchers(pf, task_name, fallback_dispatcher)
    )


def _validate_tools(dispatcher: Dispatcher, pf, target: str | None) -> list[str]:
    """Validate that all required LLM CLI tools are installed.

    Returns a list of error messages (empty if everything is fine).
    """
    errors: list[str] = []
    checked: set[str] = set()
    if isinstance(dispatcher, DryRunDispatcher):
        return errors

    def _check(d: Dispatcher, label: str) -> None:
        key = repr(
            (
                type(d).__name__,
                getattr(d, "default_model", None),
                getattr(d, "template", None),
                getattr(d, "base_url", None),
            )
        )
        if key in checked:
            return
        checked.add(key)
        err = d.validate_tool()
        if err:
            errors.append(f"{err} (needed by {label})")

    # Determine which tasks will actually execute
    if target is None:
        target = pf.default_task
    if target is None:
        return errors

    target = pf.resolve_alias(target)
    if target not in pf.tasks:
        return errors

    try:
        execution_order = _execution_task_names(pf, target)
    except CycleError:
        return errors  # cycle errors are reported later

    fallback_needed = False
    for task_name in execution_order:
        task = pf.tasks[task_name]
        # Skip shell-only tasks — they don't invoke the LLM
        has_prompt = any(s.kind == "prompt" for s in task.steps)
        has_docker = task.docker is not None
        if not has_prompt and not has_docker:
            continue

        provider_name = _provider_for_check(pf, task_name)
        if provider_name:
            provider = pf.llm_providers.get(provider_name)
            if provider is None:
                errors.append(
                    f"error: task {task_name!r} references unknown LLM provider {provider_name!r}"
                )
                continue
            _check(_dispatcher_for_provider(provider), f"task '{task_name}'")
        else:
            fallback_needed = True
        for fallback_name in task.options.fallback_llms:
            fallback_provider = pf.llm_providers.get(fallback_name)
            if fallback_provider is None:
                errors.append(
                    f"error: task {task_name!r} references unknown fallback "
                    f"LLM provider {fallback_name!r}"
                )
                continue
            _check(
                _dispatcher_for_provider(fallback_provider),
                f"fallback for task '{task_name}'",
            )

    if fallback_needed:
        _check(dispatcher, "default dispatcher")

    return errors


def _resolve_target(pf: Promptfile, target: str | None) -> str | None:
    if target is None:
        return pf.default_task
    return pf.resolve_alias(target)


def _execution_task_names(pf: Promptfile, target: str) -> list[str]:
    """Return normal execution plus failure-hook dependency closures."""
    task_names = topological_sort(pf, target)
    seen = set(task_names)
    index = 0
    while index < len(task_names):
        task = pf.tasks[task_names[index]]
        index += 1
        for hook in (task.options.postmortem, task.options.rollback):
            if not hook:
                continue
            hook = pf.resolve_alias(hook)
            for hook_task in topological_sort(pf, hook):
                if hook_task not in seen:
                    seen.add(hook_task)
                    task_names.append(hook_task)
    return task_names


def _build_task_args(
    pf: Promptfile, target: str | None, raw_args: list[str]
) -> tuple[dict[str, str] | None, str | None]:
    """Build task argument mapping for the selected target."""
    target = _resolve_target(pf, target)
    if not target or target not in pf.tasks:
        return None, None
    if not pf.tasks[target].arguments:
        if raw_args:
            return None, f"task {target!r} does not accept arguments: {' '.join(raw_args)}"
        return None, None

    task_def = pf.tasks[target]
    task_args: dict[str, str] = {}
    consumed = 0
    for idx, arg_def in enumerate(task_def.arguments):
        if arg_def.variadic:
            remaining = raw_args[idx:]
            if arg_def.variadic == "+" and not remaining:
                return None, f"task {target!r} requires at least one value for +{arg_def.name}"
            task_args[arg_def.name] = " ".join(remaining)
            consumed = len(raw_args)
            break
        if idx < len(raw_args):
            task_args[arg_def.name] = raw_args[idx]
            consumed += 1
        elif arg_def.default is not None:
            task_args[arg_def.name] = arg_def.default
        else:
            return None, f"task {target!r} requires argument {arg_def.name!r}"
    if consumed < len(raw_args):
        extras = " ".join(raw_args[consumed:])
        return None, f"task {target!r} received unexpected arguments: {extras}"
    return task_args, None


def _multi_task_targets(pf: Promptfile, task: str | None, raw_args: list[str]) -> list[str] | None:
    """Return multiple task targets when ARGS are task names, not task arguments."""
    if not task or not raw_args:
        return None
    first = pf.resolve_alias(task)
    if first not in pf.tasks or pf.tasks[first].arguments:
        return None
    targets = [first]
    for raw in raw_args:
        resolved = pf.resolve_alias(raw)
        if resolved not in pf.tasks or pf.tasks[resolved].arguments:
            return None
        targets.append(resolved)
    return targets


def _provider_label(pf: Promptfile, task_name: str) -> str:
    provider = pf.get_llm_for_task(task_name)
    if not provider:
        return "fallback"
    parts = [provider.name]
    if provider.model:
        parts.append(f"model={provider.model}")
    if provider.shell_template:
        parts.append(f"template={provider.shell_template}")
    return " ".join(parts)


def _task_plan_options(task) -> list[str]:
    """Return user-facing task options included in plan output."""
    options: list[str] = []
    if task.options.timeout:
        options.append(f"timeout={task.options.timeout}")
    if task.options.llm_timeout:
        options.append(f"llm-timeout={task.options.llm_timeout}")
    if task.options.rollback:
        options.append(f"rollback={task.options.rollback}")
    if task.options.postmortem:
        options.append(f"postmortem={task.options.postmortem}")
    if task.options.fallback_llms:
        options.append(f"fallback-llm={'|'.join(task.options.fallback_llms)}")
    if task.options.retries:
        options.append(f"retries={task.options.retries}")
    if task.options.requires:
        options.append(f"requires={'|'.join(task.options.requires)}")
    if task.options.produces:
        options.append(f"produces={task.options.produces}")
    if task.options.repair:
        options.append(f"repair={task.options.repair}")
    if len(task.options.llms) > 1:
        options.append(f"llm={'|'.join(task.options.llms)}")
    if task.options.judge:
        options.append(f"judge={task.options.judge}")
    if task.options.max_cost:
        options.append(f"max-cost={task.options.max_cost}")
    if task.options.sources:
        options.append(f"sources={','.join(task.options.sources)}")
    if task.options.outputs:
        options.append(f"outputs={','.join(task.options.outputs)}")
    if task.options.when:
        options.append(f"when={'; '.join(task.options.when)}")
    if task.options.ssh_identity:
        options.append(f"ssh-key={task.options.ssh_identity}")
    if task.options.ssh_strict_host_key_checking:
        options.append(f"ssh-strict-host-key-checking={task.options.ssh_strict_host_key_checking}")
    if task.options.secrets:
        options.append(f"secrets={task.options.secrets}")
    return options


def _promptfile_secret_values(
    pf: Promptfile,
    task_args: dict[str, str] | None = None,
) -> set[str]:
    """Return secret-like values that must not appear in CLI previews."""
    values = secret_values_from_mapping(dict(os.environ))
    values.update(
        value
        for name, value in {
            **pf.variables,
            **pf.get_exported_env(),
        }.items()
        if is_secret_name(name) and value
    )
    values.update(
        value for name, value in (task_args or {}).items() if is_secret_name(name) and value
    )
    for task in pf.tasks.values():
        values.update(
            value for name, value in task.options.env.items() if is_secret_name(name) and value
        )
        values.update(
            value for name, value in task.local_variables.items() if is_secret_name(name) and value
        )
    values.update(provider.api_key for provider in pf.llm_providers.values() if provider.api_key)
    return values


def _plan_payload(
    pf: Promptfile,
    target: str,
    task_args: dict[str, str] | None,
    promptfile_path: str,
) -> dict[str, object]:
    """Return an execution plan without running tasks."""
    order = topological_sort(pf, target)
    tasks: list[dict[str, object]] = []
    secret_values = _promptfile_secret_values(pf, task_args)
    for idx, task_name in enumerate(order, 1):
        task = pf.tasks[task_name]
        args = task_args if task_name == target else None
        host_group = pf.get_hosts_for_task(task_name)
        host_payload: dict[str, object] = {"mode": "local"}
        if host_group:
            host_payload = {
                "mode": "ssh",
                "group": host_group.name,
                "count": len(host_group.hosts),
                "parallel": task.options.ssh_parallel,
            }

        steps: list[dict[str, str]] = []
        for step in pf.resolve_steps(
            task_name,
            args,
            promptfile_path=promptfile_path,
            mask_secrets=True,
        ):
            content = step.content
            if step.kind == "prompt":
                content = step.content.splitlines()[0] if step.content else ""
            steps.append(
                {
                    "kind": step.kind,
                    "content": redact_text(content, secret_values),
                }
            )

        tasks.append(
            {
                "index": idx,
                "name": task_name,
                "provider": _provider_label(pf, task_name),
                "hosts": host_payload,
                "dependencies": list(task.dependencies),
                "subsequent_dependencies": list(task.subsequent_dependencies),
                "options": _task_plan_options(task),
                "up_to_date": up_to_date_reason(
                    task.options.sources,
                    task.options.outputs,
                    task.options.working_dir or pf.settings.working_dir,
                )
                is not None,
                "steps": steps,
            }
        )

    return {
        "target": target,
        "variables": redact_named_values({key: pf.variables[key] for key in sorted(pf.variables)}),
        "execution_order": order,
        "tasks": tasks,
    }


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_plan(
    pf: Promptfile, target: str, task_args: dict[str, str] | None, promptfile_path: str
) -> None:
    """Print an execution plan without running tasks."""
    order: list[str] = topological_sort(pf, target)
    secret_values = _promptfile_secret_values(pf, task_args)
    display_variables = redact_named_values(pf.variables)
    print(f"Plan for {target}")
    print()
    print("Variables:")
    if pf.variables:
        for key in sorted(pf.variables):
            print(f"  {key}={display_variables[key]!r}")
    else:
        print("  (none)")
    print()
    print("Execution order:")
    for idx, task_name in enumerate(order, 1):
        task = pf.tasks[task_name]
        args = task_args if task_name == target else None
        host_group = pf.get_hosts_for_task(task_name)
        host_label = "local"
        if host_group:
            host_label = f"{host_group.name} ({len(host_group.hosts)} hosts)"
            if task.options.ssh_parallel:
                host_label += ", parallel"
        options = _task_plan_options(task)

        print(f"  {idx}. {task_name}")
        print(f"     provider: {_provider_label(pf, task_name)}")
        print(f"     hosts: {host_label}")
        if task.dependencies:
            print(f"     deps: {', '.join(task.dependencies)}")
        if task.subsequent_dependencies:
            print(f"     after: {', '.join(task.subsequent_dependencies)}")
        if options:
            print(f"     options: {', '.join(options)}")
        if up_to_date_reason(
            task.options.sources,
            task.options.outputs,
            task.options.working_dir or pf.settings.working_dir,
        ):
            print("     status: up to date (would be skipped)")
        print("     steps:")
        for step in pf.resolve_steps(
            task_name,
            args,
            promptfile_path=promptfile_path,
            mask_secrets=True,
        ):
            content = redact_text(step.content, secret_values)
            if step.kind == "shell":
                print(f"       ! {content}")
            elif step.kind == "echo":
                print(f"       @echo {content}")
            else:
                first_line = content.splitlines()[0] if content else ""
                if len(first_line) > 100:
                    first_line = first_line[:97] + "..."
                print(f"       > {first_line}")


def _graph_tasks(pf: Promptfile, target: str | None) -> list[str]:
    if target:
        target = pf.resolve_alias(target)
        if target not in pf.tasks:
            raise KeyError(f"unknown task: {target!r}")
        return topological_sort(pf, target)
    return list(pf.task_order)


def _graph_payload(pf: Promptfile, target: str | None) -> dict[str, object]:
    """Return dependency graph data."""
    tasks = _graph_tasks(pf, target)
    task_set = set(tasks)
    edges = [
        {"from": dep, "to": task_name}
        for task_name in tasks
        for dep in pf.tasks[task_name].dependencies
        if dep in task_set
    ]
    edges.extend(
        {"from": task_name, "to": dep, "kind": "subsequent"}
        for task_name in tasks
        for dep in pf.tasks[task_name].subsequent_dependencies
        if dep in task_set
    )
    return {
        "target": pf.resolve_alias(target) if target else None,
        "nodes": tasks,
        "edges": edges,
    }


def _print_graph(pf: Promptfile, target: str | None, fmt: str) -> None:
    tasks = _graph_tasks(pf, target)
    task_set = set(tasks)
    if fmt == "dot":
        print("digraph makethlm {")
        for task_name in tasks:
            print(f'  "{task_name}";')
        for task_name in tasks:
            for dep in pf.tasks[task_name].dependencies:
                if dep in task_set:
                    print(f'  "{dep}" -> "{task_name}";')
            for dep in pf.tasks[task_name].subsequent_dependencies:
                if dep in task_set:
                    print(f'  "{task_name}" -> "{dep}";')
        print("}")
        return

    print("graph TD")
    for task_name in tasks:
        node_id = "task_" + "".join(ch if ch.isalnum() else "_" for ch in task_name)
        print(f'  {node_id}["{task_name}"]')
    for task_name in tasks:
        task_id = "task_" + "".join(ch if ch.isalnum() else "_" for ch in task_name)
        for dep in pf.tasks[task_name].dependencies:
            if dep in task_set:
                dep_id = "task_" + "".join(ch if ch.isalnum() else "_" for ch in dep)
                print(f"  {dep_id} --> {task_id}")
        for dep in pf.tasks[task_name].subsequent_dependencies:
            if dep in task_set:
                dep_id = "task_" + "".join(ch if ch.isalnum() else "_" for ch in dep)
                print(f"  {task_id} --> {dep_id}")


def _validate_safe_mode(
    pf: Promptfile,
    target: str,
    *,
    allow_shell: bool,
    allow_ssh: bool,
    allow_docker: bool,
    allow_llm: bool,
    allow_secrets: bool = False,
    allow_webhook: bool = False,
    allow_mcp: bool = False,
    task_args: dict[str, str] | None = None,
    dispatcher: Dispatcher | None = None,
) -> list[str]:
    """Return safe-mode violations for the target execution subgraph."""
    errors: list[str] = []
    task_names = _execution_task_names(pf, target)
    fallback_dispatcher = dispatcher or ClaudeDispatcher()

    for task_name in task_names:
        task = pf.tasks[task_name]
        has_shell = any(step.kind == "shell" for step in task.steps)
        has_prompt = any(step.kind == "prompt" for step in task.steps) or task.docker is not None
        has_ssh = has_shell and bool(task.options.on)
        has_condition_shell = any(
            condition_uses_shell(condition) for condition in task.options.when
        )
        secret_backend = task.options.secrets or pf.settings.secrets or "env"

        if task.docker and not allow_docker:
            errors.append(f"task {task_name!r} uses a docker block; pass --allow-docker")
        if has_ssh and not allow_ssh:
            errors.append(f"task {task_name!r} runs shell steps over SSH; pass --allow-ssh")
        if has_shell and not has_ssh and not allow_shell:
            errors.append(f"task {task_name!r} runs local shell steps; pass --allow-shell")
        if has_condition_shell and not allow_shell:
            errors.append(f"task {task_name!r} runs a local shell condition; pass --allow-shell")
        args = task_args if task_name == target else None
        references_secret = _task_references_secret(pf, task_name, args)
        if references_secret:
            if not allow_secrets:
                errors.append(
                    f"task {task_name!r} reads or interpolates sensitive values; "
                    "pass --allow-secrets"
                )
            if secret_backend != "env" and not allow_shell:
                errors.append(
                    f"task {task_name!r} invokes the {secret_backend!r} secret backend; "
                    "pass --allow-shell"
                )
        if has_prompt and not allow_llm:
            errors.append(f"task {task_name!r} sends prompts to an LLM; pass --allow-llm")
        if (
            has_prompt
            and _task_uses_local_execution_provider(
                pf,
                task_name,
                fallback_dispatcher,
            )
            and not allow_shell
        ):
            errors.append(
                f"task {task_name!r} uses an LLM dispatcher with local execution access; "
                "pass --allow-shell"
            )
        if task.mcp_servers and not allow_mcp:
            names = ", ".join(server.name for server in task.mcp_servers)
            errors.append(f"task {task_name!r} attaches MCP servers ({names}); pass --allow-mcp")
        if task.options.webhook and not allow_webhook:
            errors.append(f"task {task_name!r} sends a webhook; pass --allow-webhook")
    return errors


def _capability_payload(
    pf: Promptfile,
    target: str,
    task_args: dict[str, str] | None = None,
    dispatcher: Dispatcher | None = None,
) -> dict[str, object]:
    """Return a transitive, machine-readable execution capability manifest."""
    tasks = _execution_task_names(pf, target)
    capabilities: list[dict[str, str]] = []
    fallback_dispatcher = dispatcher or ClaudeDispatcher()

    def add(
        task_name: str,
        capability: str,
        reason: str,
        allow_flag: str,
    ) -> None:
        capabilities.append(
            {
                "task": task_name,
                "capability": capability,
                "reason": reason,
                "allow_flag": allow_flag,
            }
        )

    for task_name in tasks:
        task = pf.tasks[task_name]
        has_shell = any(step.kind == "shell" for step in task.steps)
        has_prompt = any(step.kind == "prompt" for step in task.steps)
        if task.docker:
            add(
                task_name,
                "docker",
                "generates and builds a Docker artifact",
                "--allow-docker",
            )
            add(
                task_name,
                "llm",
                "uses an LLM to generate a Dockerfile",
                "--allow-llm",
            )
        elif has_prompt:
            provider_names = [
                name
                for name in (
                    _provider_for_check(pf, task_name),
                    *task.options.fallback_llms,
                )
                if name
            ]
            provider_detail = f" using {' -> '.join(provider_names)}" if provider_names else ""
            add(
                task_name,
                "llm",
                f"sends one or more prompt steps to an LLM{provider_detail}",
                "--allow-llm",
            )
        if has_prompt and _task_uses_local_execution_provider(
            pf,
            task_name,
            fallback_dispatcher,
        ):
            add(
                task_name,
                "shell",
                "uses an LLM dispatcher with local execution access",
                "--allow-shell",
            )
        if has_shell and task.options.on:
            add(
                task_name,
                "ssh",
                f"runs shell steps on host group {task.options.on!r}",
                "--allow-ssh",
            )
        elif has_shell:
            add(
                task_name,
                "shell",
                "runs local shell steps",
                "--allow-shell",
            )
        if any(condition_uses_shell(condition) for condition in task.options.when):
            add(
                task_name,
                "shell",
                "evaluates a shell-backed when condition",
                "--allow-shell",
            )
        backend = task.options.secrets or pf.settings.secrets or "env"
        args = task_args if task_name == target else None
        secret_refs = _secret_refs_for_task(pf, task_name, args)
        if secret_refs:
            reference_detail = ", ".join(secret_refs)
            add(
                task_name,
                "secrets",
                f"reads sensitive input via {backend!r}: {reference_detail}",
                "--allow-secrets",
            )
            if backend != "env":
                add(
                    task_name,
                    "shell",
                    f"invokes the external {backend!r} secret backend",
                    "--allow-shell",
                )
        for server in task.mcp_servers:
            add(
                task_name,
                "mcp",
                f"attaches MCP server {server.name!r}"
                + (f" at {server.url}" if server.url else f" running {server.command!r}"),
                "--allow-mcp",
            )
        if task.options.webhook:
            add(
                task_name,
                "webhook",
                "sends completion data to its configured webhook",
                "--allow-webhook",
            )

    return {
        "target": target,
        "tasks": tasks,
        "capabilities": capabilities,
        "required": sorted({item["capability"] for item in capabilities}),
    }


def _print_capabilities(payload: dict[str, Any]) -> None:
    """Print an execution capability manifest."""
    print(f"Capabilities for {payload['target']}")
    capabilities = payload["capabilities"]
    if not capabilities:
        print("  none")
        return
    for item in capabilities:
        print(f"  {item['capability']:<8} {item['task']}: {item['reason']} ({item['allow_flag']})")


def _check_issue(
    severity: str,
    code: str,
    message: str,
    *,
    task: str | None = None,
) -> dict[str, str]:
    issue = {"severity": severity, "code": code, "message": message}
    if task:
        issue["task"] = task
    return issue


def _task_references_secret(
    pf: Promptfile,
    task_name: str,
    args: dict[str, str] | None = None,
) -> bool:
    """Return True if a task/guidance/agent includes secret placeholders."""
    return pf.task_references_secret(task_name, args)


def _secret_refs_for_task(
    pf: Promptfile,
    task_name: str,
    args: dict[str, str] | None = None,
) -> list[str]:
    """Return secret reference names used by a task."""
    return sorted(pf.task_secret_references(task_name, args))


def _provider_for_check(pf: Promptfile, task_name: str) -> str | None:
    """Return the provider name a prompt/docker task will use."""
    agent = pf.get_agent_for_task(task_name)
    if agent and agent.llm:
        return agent.llm
    task = pf.tasks[task_name]
    return task.options.llm or pf.default_llm


def _check_promptfile(
    pf: Promptfile,
    *,
    promptfile_path: str,
) -> dict[str, object]:
    """Validate parsed Promptfile references, tools, and risky capabilities."""
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def error(code: str, message: str, *, task: str | None = None) -> None:
        errors.append(_check_issue("error", code, message, task=task))

    def warn(code: str, message: str, *, task: str | None = None) -> None:
        warnings.append(_check_issue("warning", code, message, task=task))

    if not pf.tasks:
        error("no-tasks", "no tasks defined in Promptfile")

    used_providers: set[str] = set()
    fallback_claude_needed = False
    used_secret_backends: dict[str, set[str]] = {}
    used_sandboxes: set[str] = set()
    ssh_needed = False

    for task_name in pf.task_order:
        task = pf.tasks[task_name]
        has_shell = any(step.kind == "shell" for step in task.steps)
        has_prompt = any(step.kind == "prompt" for step in task.steps)
        needs_llm = has_prompt or task.docker is not None

        for provider_name in task.options.llms or ([task.options.llm] if task.options.llm else []):
            if provider_name not in pf.llm_providers:
                error(
                    "unknown-provider",
                    f"task {task_name!r} references unknown LLM provider {provider_name!r}",
                    task=task_name,
                )
        if task.options.judge and task.options.judge not in pf.llm_providers:
            error(
                "unknown-provider",
                f"task {task_name!r} references unknown judge provider {task.options.judge!r}",
                task=task_name,
            )

        agent = pf.get_agent_for_task(task_name)
        if agent and agent.llm and agent.llm not in pf.llm_providers:
            error(
                "unknown-agent-provider",
                f"agent {agent.name!r} references unknown LLM provider {agent.llm!r}",
                task=task_name,
            )

        if needs_llm:
            resolved_provider = _provider_for_check(pf, task_name)
            if resolved_provider and resolved_provider in pf.llm_providers:
                used_providers.add(resolved_provider)
            elif not resolved_provider:
                fallback_claude_needed = True
            used_providers.update(task.options.fallback_llms)

        if has_shell:
            if task.options.on:
                ssh_needed = True
                warn(
                    "ssh-execution",
                    f"task {task_name!r} runs shell steps over SSH",
                    task=task_name,
                )
            else:
                warn(
                    "shell-execution",
                    f"task {task_name!r} runs local shell steps",
                    task=task_name,
                )

        if task.docker:
            warn(
                "docker-generation",
                f"task {task_name!r} generates/builds Docker artifacts",
                task=task_name,
            )

        sandbox = task.options.sandbox or pf.settings.sandbox
        if sandbox and sandbox != "none" and has_shell:
            used_sandboxes.add(sandbox)
        if task.options.sandbox_net == "host":
            warn(
                "sandbox-host-network", f"task {task_name!r} uses sandbox-net=host", task=task_name
            )

        if _task_references_secret(pf, task_name):
            backend = task.options.secrets or pf.settings.secrets or "env"
            used_secret_backends.setdefault(backend, set()).update(
                _secret_refs_for_task(pf, task_name)
            )

    for provider_name in sorted(used_providers):
        provider = pf.llm_providers[provider_name]
        if provider.name.lower() not in _KNOWN_NATIVE_PROVIDERS and not provider.shell_template:
            warn(
                "provider-fallback",
                (
                    f"provider {provider_name!r} has no native dispatcher or template; "
                    "it will use the Claude CLI fallback"
                ),
            )
        tool_error = _dispatcher_for_provider(provider).validate_tool()
        if tool_error:
            error("missing-provider-tool", tool_error)

    if fallback_claude_needed:
        tool_error = ClaudeDispatcher().validate_tool()
        if tool_error:
            error("missing-provider-tool", f"{tool_error} (fallback dispatcher)")

    if ssh_needed and shutil.which("ssh") is None:
        error("missing-ssh-tool", "ssh CLI not found on PATH")

    for sandbox in sorted(used_sandboxes):
        tool = _SANDBOX_TOOLS.get(sandbox)
        if tool and shutil.which(tool) is None:
            error(
                "missing-sandbox-tool",
                f"{tool!r} required for sandbox {sandbox!r} was not found on PATH",
            )

    base_dir = os.path.dirname(os.path.abspath(promptfile_path))
    for backend, refs in sorted(used_secret_backends.items()):
        if backend == "env":
            for ref in sorted(refs):
                if ref not in os.environ and ref.replace("/", "_") not in os.environ:
                    error("missing-env-secret", f"environment secret {ref!r} is not set")
            continue
        tool = _SECRETS_BACKEND_TOOLS.get(backend)
        if tool is None:
            error("unknown-secrets-backend", f"unknown secrets backend {backend!r}")
            continue
        if shutil.which(tool) is None:
            error(
                "missing-secrets-tool",
                f"{tool!r} required for secrets backend {backend!r} was not found on PATH",
            )
        if backend == "sops":
            if not pf.settings.secrets_file:
                error("missing-sops-file", "sops secrets require set secrets-file")
            else:
                secrets_file = os.path.normpath(os.path.join(base_dir, pf.settings.secrets_file))
                if not os.path.isfile(secrets_file):
                    error("missing-sops-file", f"sops secrets file not found: {secrets_file}")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "tasks": len(pf.tasks),
            "errors": len(errors),
            "warnings": len(warnings),
        },
    }


def _print_check_result(payload: dict[str, Any]) -> None:
    """Print check results in a concise human-readable format."""
    for issue in payload["errors"]:
        print(f"error[{issue['code']}]: {issue['message']}")
    for issue in payload["warnings"]:
        print(f"warning[{issue['code']}]: {issue['message']}")
    summary = payload["summary"]
    if payload["ok"]:
        print(f"OK: {summary['tasks']} task(s), {summary['warnings']} warning(s)")
    else:
        print(f"FAILED: {summary['errors']} error(s), {summary['warnings']} warning(s)")


def _history_payload(limit_raw: str | None = None) -> dict[str, Any]:
    """Return recent local run history."""
    try:
        limit = int(limit_raw or "20")
    except ValueError:
        limit = 20
    rows = list_runs(limit=max(1, limit))
    runs = []
    for row in rows:
        item = dict(row)
        item["success"] = bool(item["success"])
        try:
            item["tasks"] = json.loads(item.pop("tasks_json"))
        except (TypeError, json.JSONDecodeError):
            item["tasks"] = []
        runs.append(item)
    return {"runs": runs}


def _print_history(limit_raw: str | None = None) -> None:
    """Print recent local run history."""
    rows = _history_payload(limit_raw)["runs"]
    if not rows:
        print("No runs recorded.")
        return
    for row in rows:
        status = "ok" if row["success"] else "FAILED"
        cost = row.get("cost_usd") or 0.0
        spend = f"  ${cost:.4f}" if cost else ""
        print(
            f"{row['id']:>4}  {status:<6}  {row['target']:<20} "
            f"{row['duration_ms']}ms{spend}  {row['started_at']}"
        )


def _print_replay(bundle: dict[str, Any]) -> None:
    """Print a recorded run bundle without re-executing any task."""
    status = "ok" if bundle["success"] else "FAILED"
    print(f"Run {bundle['id']}  {status}  {bundle['target']}  {bundle['duration_ms']}ms")
    print(f"Started: {bundle['started_at']}")
    if bundle.get("promptfile"):
        print(f"Promptfile: {bundle['promptfile']}")
    for task in bundle["tasks"]:
        task_status = "ok" if task.get("success") else "FAILED"
        print(f"\n[{task_status}] {task.get('task', '(unknown)')}")
        response = str(task.get("response", "")).strip()
        for line in response.splitlines():
            print(f"  {line}")


def _task_args_display(task) -> str:
    parts = []
    for arg in task.arguments:
        prefix = arg.variadic or ""
        if arg.default is not None:
            default = "[redacted]" if is_secret_name(arg.name) else arg.default
            parts.append(f'{prefix}{arg.name}="{default}"')
        else:
            parts.append(f"{prefix}{arg.name}")
    return ", ".join(parts)


def _task_description(task) -> str:
    desc = task.options.doc or task.prompt.split("\n")[0]
    if len(desc) > 60:
        desc = desc[:57] + "..."
    return desc


def _task_attributes(task) -> list[str]:
    attrs: list[str] = []
    options = task.options
    if options.default:
        attrs.append("default")
    if options.confirm:
        attrs.append("confirm")
    if options.script:
        attrs.append("script")
    if options.script_command:
        attrs.append(f"script={options.script_command}")
    if options.extension:
        attrs.append(f"extension={options.extension}")
    if options.metadata:
        attrs.append("metadata")
    if options.env_enabled:
        attrs.append("env")
    if options.cache:
        attrs.append(f"cache={options.cache}")
    if options.sources:
        attrs.append(f"sources={','.join(options.sources)}")
    if options.outputs:
        attrs.append(f"outputs={','.join(options.outputs)}")
    if options.timeout:
        attrs.append(f"timeout={options.timeout}")
    if options.llm_timeout:
        attrs.append(f"llm-timeout={options.llm_timeout}")
    if options.rollback:
        attrs.append(f"rollback={options.rollback}")
    if options.sandbox:
        attrs.append(f"sandbox={options.sandbox}")
    if options.sandbox_read_only:
        attrs.append("sandbox-read-only")
    if options.ssh_parallel:
        attrs.append("ssh-parallel")
    if options.webhook:
        attrs.append(f"webhook-on={options.webhook_on}")
    if options.when:
        attrs.append("when")
    if options.produces:
        attrs.append(f"produces={options.produces}")
    if options.repair:
        attrs.append(f"repair={options.repair}")
    return attrs


def _list_payload(pf: Promptfile) -> dict[str, object]:
    aliases_by_target: dict[str, list[str]] = {}
    for alias_name, target in pf.aliases.items():
        aliases_by_target.setdefault(target, []).append(alias_name)

    tasks = []
    for name in pf.task_order:
        task = pf.tasks[name]
        if task.options.private:
            continue
        module = name.split("::", 1)[0] if "::" in name else None
        tasks.append(
            {
                "name": name,
                "module": module,
                "group": task.options.group,
                "description": _task_description(task),
                "aliases": aliases_by_target.get(name, []),
                "attributes": _task_attributes(task),
                "dependencies": task.dependencies,
                "subsequent_dependencies": task.subsequent_dependencies,
                "arguments": _task_args_display(task).split(", ") if task.arguments else [],
                "docker": task.docker.tag if task.docker else None,
                "llm": task.options.llm,
                "agent": task.options.agent,
                "on": task.options.on,
            }
        )

    return {
        "tasks": tasks,
        "aliases": pf.aliases,
        "functions": sorted(pf.functions),
        "llm_providers": sorted(pf.llm_providers),
        "host_groups": sorted(pf.host_groups),
        "agents": sorted(pf.agents),
    }


def _print_list(pf: Promptfile) -> None:
    aliases_by_target: dict[str, list[str]] = {}
    for alias_name, target in pf.aliases.items():
        aliases_by_target.setdefault(target, []).append(alias_name)

    ungrouped: list[str] = []
    groups: dict[str, list[str]] = {}
    modules: dict[str, list[str]] = {}
    for name in pf.task_order:
        task = pf.tasks[name]
        if task.options.private:
            continue
        if "::" in name:
            modules.setdefault(name.split("::", 1)[0], []).append(name)
        elif task.options.group:
            groups.setdefault(task.options.group, []).append(name)
        else:
            ungrouped.append(name)

    def _print_task(name: str, indent: str = "  ") -> None:
        task = pf.tasks[name]
        parts: list[str] = []
        if task.dependencies:
            parts.append(f"depends: {', '.join(task.dependencies)}")
        if task.subsequent_dependencies:
            parts.append(f"then: {', '.join(task.subsequent_dependencies)}")
        if task.arguments:
            parts.append(f"args: {_task_args_display(task)}")
        if aliases_by_target.get(name):
            parts.append(f"aliases: {', '.join(aliases_by_target[name])}")
        if task.docker:
            parts.append(f"docker: {task.docker.tag}")
        if task.options.llm:
            parts.append(f"llm: {task.options.llm}")
        if task.options.agent:
            parts.append(f"agent: {task.options.agent}")
        if task.options.on:
            parts.append(f"on: {task.options.on}")
        if task.options.secrets:
            parts.append(f"secrets: {task.options.secrets}")
        if task.options.os_filter:
            parts.append(f"os: {task.options.os_filter}")
        attrs = _task_attributes(task)
        if attrs:
            parts.append(f"attrs: {', '.join(attrs)}")
        suffix = f" ({'; '.join(parts)})" if parts else ""
        print(f"{indent}{name}{suffix}")
        print(f"{indent}  {_task_description(task)}")

    for name in ungrouped:
        _print_task(name)

    for group_name, task_names in groups.items():
        print()
        print(f"  [{group_name}]")
        for name in task_names:
            _print_task(name)

    if modules:
        print()
        print("  modules:")
        for module_name, task_names in modules.items():
            print(f"    [{module_name}]")
            for name in task_names:
                _print_task(name, indent="      ")

    if pf.aliases:
        print()
        print("  aliases:")
        for alias_name, alias_target in pf.aliases.items():
            print(f"    {alias_name} -> {alias_target}")

    if pf.functions:
        print()
        print("  functions:")
        for fn_name, fn in pf.functions.items():
            first_line = fn.body.split("\n")[0]
            if len(first_line) > 60:
                first_line = first_line[:57] + "..."
            print(f"    {fn_name}: {first_line}")

    if pf.llm_providers:
        print()
        default = pf.default_llm
        print("  llm providers:")
        for pname, prov in pf.llm_providers.items():
            marker = " (default)" if pname == default else ""
            model_str = f" model={prov.model}" if prov.model else ""
            print(f"    {pname}{model_str}{marker}")

    if pf.host_groups:
        print()
        print("  host groups:")
        for gname, group in pf.host_groups.items():
            user_str = f" user={group.user}" if group.user else ""
            port_str = f" port={group.port}" if group.port else ""
            print(f"    {gname}{user_str}{port_str}: {', '.join(group.hosts)}")

    if pf.guidance:
        print()
        first_line = pf.guidance.split("\n")[0]
        if len(first_line) > 60:
            first_line = first_line[:57] + "..."
        print(f"  guidance: {first_line}")

    if pf.agents:
        print()
        print("  agents:")
        for aname, agent in pf.agents.items():
            parts = []
            if agent.llm:
                parts.append(f"llm={agent.llm}")
            if agent.model:
                parts.append(f"model={agent.model}")
            suffix = f" ({', '.join(parts)})" if parts else ""
            first_line = agent.instructions.split("\n")[0]
            if len(first_line) > 60:
                first_line = first_line[:57] + "..."
            print(f"    {aname}{suffix}: {first_line}")


def _completion_script(shell: str) -> str:
    """Return a shell completion script for makethlm."""
    common_opts = (
        "--file -f --dry-run --json --parallel --jobs --list -l --summary "
        "--dump --check --capabilities --plan --graph --graph-format "
        "--history --no-history "
        "--evaluate --model -m --shell --codex --openai --ollama --safe "
        "--allow-backticks --allow-shell --allow-ssh --allow-docker --allow-llm "
        "--allow-secrets "
        "--allow-webhook "
        "--var -V --quiet -q --verbose"
    )
    if shell == "bash":
        return f"""# bash completion for makethlm
_makethlm_complete() {{
    local cur="${{COMP_WORDS[COMP_CWORD]}}"
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "{common_opts} completions history replay" -- "$cur") )
        return 0
    fi
    local tasks
    tasks="$(makethlm --summary 2>/dev/null || true)"
    COMPREPLY=( $(compgen -W "completions history replay $tasks" -- "$cur") )
}}
complete -F _makethlm_complete makethlm
"""
    if shell == "zsh":
        return f"""#compdef makethlm
# zsh completion for makethlm
_makethlm() {{
  local -a opts tasks
  opts=(${{=:-"{common_opts} completions history replay"}})
  tasks=(${{(f)"$(makethlm --summary 2>/dev/null)"}})
  _describe 'option' opts
  _describe 'task' tasks
}}
_makethlm "$@"
"""
    if shell == "fish":
        lines = [
            "# fish completion for makethlm",
            "function __makethlm_tasks",
            "    makethlm --summary 2>/dev/null",
            "end",
        ]
        for opt in common_opts.split():
            if opt.startswith("--"):
                lines.append(f"complete -c makethlm -l {opt[2:]}")
            elif opt.startswith("-") and len(opt) == 2:
                lines.append(f"complete -c makethlm -s {opt[1:]}")
        lines.append("complete -c makethlm -a '(__makethlm_tasks)'")
        lines.append("complete -c makethlm -a 'completions history replay'")
        return "\n".join(lines) + "\n"
    raise ValueError(f"unsupported shell: {shell!r} (expected bash, zsh, or fish)")


def _step_result_payload(step: StepResult) -> dict[str, object]:
    """Return a JSON-safe step result payload."""
    return {
        "kind": step.kind,
        "content": step.content,
        "response": step.response,
        "success": step.success,
        "exit_code": step.exit_code if step.exit_code is not None else (0 if step.success else 1),
        "host": step.host,
        "provider": step.provider,
        "attempt": step.attempt,
    }


def _task_result_payload(task: TaskResult) -> dict[str, object]:
    """Return a JSON-safe task result payload."""
    return {
        "task": task.task_name,
        "success": task.success,
        "exit_code": 0 if task.success else 1,
        "prompt": task.prompt_sent,
        "response": task.response,
        "steps": [_step_result_payload(step) for step in task.step_results],
    }


def _run_result_payload(
    result: RunResult,
    *,
    duration_ms: int,
    promptfile_path: str | None,
    dry_run: bool,
    parallel: bool,
    jobs: int | None,
    run_id: int | None = None,
    costs: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a JSON-safe run result payload."""
    payload: dict[str, object] = {
        "target": result.target,
        "success": result.success,
        "exit_code": 0 if result.success else 1,
        "duration_ms": duration_ms,
        "promptfile": promptfile_path,
        "dry_run": dry_run,
        "parallel": parallel,
        "jobs": jobs,
        "tasks": [_task_result_payload(task) for task in result.task_results],
    }
    if costs is not None:
        payload["costs"] = costs
    if run_id is not None:
        payload["run_id"] = run_id
    return payload


def _run_fmt(args: argparse.Namespace) -> int:
    """Format Promptfiles in place, or check them without writing."""
    paths: list[Path] = [Path(item) for item in args.task_args]
    if not paths:
        discovered = args.file or find_promptfile()
        if discovered is None:
            print("error: no Promptfile found", file=sys.stderr)
            return 1
        paths = [discovered]

    unformatted: list[Path] = []
    for path in paths:
        if not path.is_file():
            print(f"error: {path} is not a file", file=sys.stderr)
            return 1
        original = path.read_text()
        formatted = format_text(original)
        if formatted == original:
            continue
        unformatted.append(path)
        if not args.check:
            path.write_text(formatted)

    if args.check:
        for path in unformatted:
            print(f"would reformat: {path}")
        if unformatted:
            count = len(unformatted)
            print(f"{count} file{'s' if count != 1 else ''} would be reformatted")
            return 1
        print("all files are formatted")
        return 0

    for path in unformatted:
        print(f"reformatted: {path}")
    if not unformatted:
        print("all files are already formatted")
    return 0


def _watched_patterns(pf: Promptfile, target: str) -> list[str]:
    """Return the source patterns to watch for a target and its dependencies."""
    patterns: list[str] = []
    try:
        order = topological_sort(pf, target)
    except (KeyError, CycleError):
        order = [target] if target in pf.tasks else []
    for task_name in order:
        for pattern in pf.tasks[task_name].options.sources:
            if pattern not in patterns:
                patterns.append(pattern)
    return patterns


def _watch_loop(
    run_once: Callable[[], int],
    patterns: list[str],
    promptfile_path: str,
    interval: float,
) -> int:
    """Run a target, then re-run it whenever a watched file changes."""
    watched = [*patterns, promptfile_path]

    def snapshot() -> str | None:
        return digest_sources(watched)

    exit_code = run_once()
    # Watch output is usually piped or scrolled; keep it readable as it happens.
    sys.stdout.flush()
    last = snapshot()
    print(
        f"watching {len(patterns)} source pattern(s); press Ctrl-C to stop",
        file=sys.stderr,
    )
    try:
        while True:
            time.sleep(max(interval, 0.05))
            current = snapshot()
            if current == last:
                continue
            last = current
            print("change detected; re-running", file=sys.stderr)
            exit_code = run_once()
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return exit_code


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.jobs is not None and args.jobs < 1:
        print("error: --jobs must be at least 1", file=sys.stderr)
        return 1
    if args.jobs is not None:
        args.parallel = True

    if args.since:
        os.environ[SINCE_ENV_VAR] = args.since

    if args.record_fixtures and not args.fixtures:
        print("error: --record-fixtures requires --fixtures DIR", file=sys.stderr)
        return 1

    max_cost: float | None = None
    if args.max_cost is not None:
        try:
            max_cost = parse_cost(args.max_cost)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    if args.history is not None:
        if args.json_output:
            _print_json(_history_payload(args.history))
        else:
            _print_history(args.history)
        return 0

    if args.task == "history":
        limit = args.task_args[0] if args.task_args else "20"
        if args.json_output:
            _print_json(_history_payload(limit))
        else:
            _print_history(limit)
        return 0

    if args.task == "replay":
        if len(args.task_args) != 1:
            print("error: replay requires exactly one run ID", file=sys.stderr)
            return 1
        try:
            run_id = int(args.task_args[0])
        except ValueError:
            print("error: replay run ID must be an integer", file=sys.stderr)
            return 1
        bundle = get_run(run_id)
        if bundle is None:
            print(f"error: run {run_id} was not found", file=sys.stderr)
            return 1
        if args.json_output:
            _print_json(bundle)
        else:
            _print_replay(bundle)
        return 0

    if args.task == "fmt":
        return _run_fmt(args)

    if args.task in ("completions", "completion"):
        if not args.task_args:
            print("error: completions requires a shell: bash, zsh, or fish", file=sys.stderr)
            return 1
        shell = args.task_args[0]
        try:
            print(_completion_script(shell), end="")
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    # Locate the Promptfile
    pf_path: Path | None = args.file
    if pf_path is None:
        pf_path = find_promptfile()
    if pf_path is None or not pf_path.is_file():
        print("error: no Promptfile found", file=sys.stderr)
        return 1

    # Parse
    try:
        source = pf_path.read_text()
        inspection_mode = any(
            (
                args.check,
                args.capabilities,
                args.plan,
                args.graph,
                args.dump,
                args.list_tasks,
                args.summary,
                args.dry_run,
                args.evaluate is not None,
            )
        )
        allow_backticks = args.allow_backticks or not args.safe and not inspection_mode
        pf = parse(source, filename=str(pf_path), allow_backticks=allow_backticks)
    except ParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Apply variable overrides from CLI
    for var_str in args.var:
        if "=" not in var_str:
            print(
                f"error: invalid --var format (expected name=value): {var_str!r}", file=sys.stderr
            )
            return 1
        key, value = var_str.split("=", 1)
        pf.variables[key.strip()] = value.strip()

    # A CLI model selection is an explicit run-wide override.
    if args.model:
        for task in pf.tasks.values():
            task.options.model = args.model

    # --evaluate: evaluate an expression
    if args.evaluate is not None:
        context = _builtin_functions()
        context.update(pf.variables)
        result = _evaluate_expression(args.evaluate, context)
        print(result)
        return 0

    if args.check:
        payload = _check_promptfile(pf, promptfile_path=str(pf_path))
        if args.json_output:
            _print_json(payload)
        else:
            _print_check_result(payload)
        return 0 if payload["ok"] else 1

    # --summary: compact task list
    if args.summary:
        for name in pf.task_order:
            task = pf.tasks[name]
            if not task.options.private:
                print(name)
        return 0

    # --dump: dump parsed structure
    if args.dump:
        print("variables:")
        display_variables = redact_named_values(pf.variables)
        for k, v in display_variables.items():
            exported = " (exported)" if k in pf.exported_vars else ""
            print(f"  {k} = {v!r}{exported}")
        if pf.settings.export:
            print("  (set export: all variables exported)")
        print()
        print("settings:")
        for field_name in (
            "dotenv_load",
            "secrets",
            "secrets_project",
            "secrets_environment",
            "secrets_vault",
            "secrets_file",
            "shell",
            "working_dir",
            "export",
            "positional_arguments",
            "ignore_comments",
            "tempdir",
            "quiet",
        ):
            val = getattr(pf.settings, field_name)
            if val:
                print(f"  {field_name} = {val!r}")
        print()
        if pf.guidance:
            print("guidance:")
            for gline in pf.guidance.split("\n")[:5]:
                print(f"  {gline}")
            if pf.guidance.count("\n") > 4:
                print(f"  ... ({pf.guidance.count(chr(10)) + 1} lines total)")
            print()
        if pf.agents:
            print("agents:")
            for aname, agent in pf.agents.items():
                parts = [agent.instructions_path]
                if agent.llm:
                    parts.append(f"llm={agent.llm}")
                if agent.model:
                    parts.append(f"model={agent.model}")
                print(f"  {aname} ({', '.join(parts)})")
            print()
        print("tasks:")
        for name in pf.task_order:
            task = pf.tasks[name]
            flags = []
            if task.options.private:
                flags.append("private")
            if task.options.agent:
                flags.append(f"agent={task.options.agent}")
            if task.options.os_filter:
                flags.append(f"os={task.options.os_filter}")
            if task.options.no_cd:
                flags.append("no-cd")
            if task.options.confirm:
                flags.append("confirm")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            deps_str = f" : {' '.join(task.dependencies)}" if task.dependencies else ""
            args_str = ""
            if task.arguments:
                args_str = f"({_task_args_display(task)})"
            print(f"  {name}{args_str}{deps_str}{flag_str}")
        return 0

    # List mode
    if args.list_tasks:
        if args.json_output:
            _print_json(_list_payload(pf))
        else:
            _print_list(pf)
        return 0

    target = _resolve_target(pf, args.task)
    configured_dispatcher = _build_dispatcher(args, honor_dry_run=False)

    if args.capabilities:
        if target is None:
            print("error: no tasks defined in Promptfile", file=sys.stderr)
            return 1
        if target not in pf.tasks:
            print(f"error: unknown task: {target!r}", file=sys.stderr)
            return 1
        capability_args, arg_error = _build_task_args(pf, args.task, args.task_args)
        if arg_error:
            print(f"error: {arg_error}", file=sys.stderr)
            return 1
        try:
            payload = _capability_payload(
                pf,
                target,
                capability_args,
                configured_dispatcher,
            )
        except CycleError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if args.json_output:
            _print_json(payload)
        else:
            _print_capabilities(payload)
        return 0

    if args.graph:
        try:
            graph_target = target if args.task else None
            if args.json_output:
                _print_json(_graph_payload(pf, graph_target))
            else:
                _print_graph(pf, graph_target, args.graph_format)
        except (KeyError, CycleError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    multi_targets = _multi_task_targets(pf, args.task, args.task_args)
    if multi_targets and args.plan:
        print("error: --plan does not support multiple task invocation", file=sys.stderr)
        return 1

    if multi_targets:
        task_args = None
        arg_error = None
    else:
        task_args, arg_error = _build_task_args(pf, args.task, args.task_args)
    if arg_error:
        print(f"error: {arg_error}", file=sys.stderr)
        return 1

    if args.plan:
        if target is None:
            print("error: no tasks defined in Promptfile", file=sys.stderr)
            return 1
        if target not in pf.tasks:
            print(f"error: unknown task: {target!r}", file=sys.stderr)
            return 1
        try:
            if args.json_output:
                _print_json(_plan_payload(pf, target, task_args, str(pf_path)))
            else:
                _print_plan(pf, target, task_args, str(pf_path))
        except CycleError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    dispatcher = _build_dispatcher(args)

    # Validate that required CLI tools are installed
    tool_errors: list[str] = []
    for validation_target in multi_targets or [args.task]:
        tool_errors.extend(_validate_tools(dispatcher, pf, validation_target))
    if tool_errors:
        for err in tool_errors:
            print(err, file=sys.stderr)
        return 1

    if args.safe and not args.dry_run:
        safe_targets: list[str | None] = list(multi_targets) if multi_targets else [target]
        if any(safe_target is None for safe_target in safe_targets):
            print("error: no tasks defined in Promptfile", file=sys.stderr)
            return 1
        try:
            safe_errors = []
            for safe_target in safe_targets:
                if safe_target is None:
                    print("error: no tasks defined in Promptfile", file=sys.stderr)
                    return 1
                if safe_target not in pf.tasks:
                    print(f"error: unknown task: {safe_target!r}", file=sys.stderr)
                    return 1
                safe_errors.extend(
                    _validate_safe_mode(
                        pf,
                        safe_target,
                        allow_shell=args.allow_shell,
                        allow_ssh=args.allow_ssh,
                        allow_docker=args.allow_docker,
                        allow_llm=args.allow_llm,
                        allow_secrets=args.allow_secrets,
                        allow_webhook=args.allow_webhook,
                        allow_mcp=args.allow_mcp,
                        task_args=task_args if safe_target == target else None,
                        dispatcher=configured_dispatcher,
                    )
                )
        except CycleError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if safe_errors:
            for err in safe_errors:
                print(f"error: safe mode blocked execution: {err}", file=sys.stderr)
            return 1

    def _execute_once() -> int:
        """Build a runner, execute the target, and report the outcome."""
        runner = Runner(
            pf,
            dispatcher,
            quiet=args.quiet,
            verbose=args.verbose and not args.dry_run and not args.json_output,
            promptfile_path=str(pf_path),
            dry_run=args.dry_run,
            always_make=args.always_make,
            fixtures_dir=args.fixtures,
            record_fixtures=args.record_fixtures,
            max_cost=max_cost,
            call_log_path=args.log_llm,
        )
        try:
            started = time.monotonic()
            if multi_targets:
                result = RunResult(target=" ".join(multi_targets))
                for multi_target in multi_targets:
                    part = (
                        runner.run_parallel(multi_target, jobs=args.jobs)
                        if args.parallel
                        else runner.run(multi_target)
                    )
                    result.task_results.extend(part.task_results)
                    if not part.success:
                        break
            elif args.parallel:
                result = runner.run_parallel(target, args=task_args, jobs=args.jobs)
            else:
                result = runner.run(target, args=task_args)
            duration_ms = int((time.monotonic() - started) * 1000)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 130
        except SecretError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except KeyError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except CycleError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        run_id = None
        if not args.dry_run and not args.no_history:
            run_id = record_run(
                result,
                duration_ms=duration_ms,
                promptfile_path=str(pf_path),
                redact=runner._redact,
                costs=runner.costs.as_dict(),
            )

        if args.json_output:
            _print_json(
                _run_result_payload(
                    result,
                    duration_ms=duration_ms,
                    promptfile_path=str(pf_path),
                    dry_run=args.dry_run,
                    parallel=args.parallel,
                    jobs=args.jobs,
                    run_id=run_id,
                    costs=runner.costs.as_dict(),
                )
            )
            return 0 if result.success else 1

        # Print results
        for tr in result.task_results:
            status = "ok" if tr.success else "FAILED"
            print(f"[{status}] {tr.task_name}")
            if args.dry_run:
                for sr in tr.step_results:
                    prefix = "!" if sr.kind == "shell" else ">"
                    print(f"  {prefix} {sr.content}")
            else:
                for line in tr.response.strip().split("\n"):
                    if line:
                        print(f"  {line}")
            print()

        if runner.costs.has_data and not args.quiet:
            print(f"usage: {runner.costs.summary()}", file=sys.stderr)

        return 0 if result.success else 1

    if not args.watch:
        return _execute_once()

    watch_target = multi_targets[0] if multi_targets else target
    if watch_target is None:
        print("error: no task to watch", file=sys.stderr)
        return 1
    watched = _watched_patterns(pf, watch_target)
    if not watched:
        print(
            "error: --watch needs at least one task with sources= in the target's "
            "dependency closure",
            file=sys.stderr,
        )
        return 1
    return _watch_loop(_execute_once, watched, str(pf_path), args.watch_interval)


if __name__ == "__main__":
    raise SystemExit(main())
