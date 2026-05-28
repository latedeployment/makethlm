"""CLI entry point for makethlm."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .parser import parse, ParseError
from .runner import (
    Runner,
    CycleError,
    RunResult,
    StepResult,
    TaskResult,
    topological_sort,
    _dispatcher_for_provider,
)
from .dispatcher import ClaudeDispatcher, CodexDispatcher, Dispatcher, DryRunDispatcher, ShellDispatcher
from .models import Promptfile, SecretError, _evaluate_expression, _builtin_functions
from .history import list_runs, record_run


PROMPTFILE_NAMES = ["Promptfile", "promptfile", "Promptfile.pf", "promptfile.pf"]

_KNOWN_NATIVE_PROVIDERS = {"claude", "codex"}
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
    """Search for a Promptfile in the given directory (default: cwd)."""
    d = directory or Path.cwd()
    for name in PROMPTFILE_NAMES:
        candidate = d / name
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
        "-f", "--file",
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
        "--jobs",
        type=int,
        default=None,
        help="Maximum number of parallel task workers",
    )
    ap.add_argument(
        "--list", "-l",
        action="store_true",
        dest="list_tasks",
        help="List available tasks and exit",
    )
    ap.add_argument(
        "--summary", "-s",
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
        "--serve",
        nargs="?",
        const="127.0.0.1:8765",
        default=None,
        help="Serve a small local task UI/API, optionally HOST:PORT",
    )
    ap.add_argument(
        "--evaluate",
        metavar="EXPR",
        default=None,
        help="Evaluate an expression and print the result",
    )
    ap.add_argument(
        "--model", "-m",
        default=None,
        help="Default LLM model to use",
    )
    ap.add_argument(
        "--shell",
        default=None,
        help='Shell template for LLM CLI, e.g. \'openai chat -p "{prompt}"\'',
    )
    ap.add_argument(
        "--codex",
        action="store_true",
        help="Use the Codex CLI as the default LLM dispatcher",
    )
    ap.add_argument(
        "--safe",
        action="store_true",
        help="Enable restrictive safety checks before execution",
    )
    ap.add_argument(
        "--allow-backticks",
        action="store_true",
        help="Allow Promptfile backtick command substitution in safe mode",
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
        "--var", "-V",
        action="append",
        default=[],
        help="Override a variable: -V name=value",
    )
    ap.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress command echoing",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output (show all step details)",
    )
    return ap


def _validate_tools(dispatcher: Dispatcher, pf, target: str | None) -> list[str]:
    """Validate that all required LLM CLI tools are installed.

    Returns a list of error messages (empty if everything is fine).
    """
    errors: list[str] = []
    checked: set[str] = set()

    def _check(d: Dispatcher, label: str) -> None:
        key = repr(d)
        if key in checked:
            return
        checked.add(key)
        err = d.validate_tool()
        if err:
            errors.append(f"{err} (needed by {label})")

    # Validate the default dispatcher
    _check(dispatcher, "default dispatcher")

    # Determine which tasks will actually execute
    if target is None:
        target = pf.default_task
    if target is None:
        return errors

    target = pf.resolve_alias(target)
    if target not in pf.tasks:
        return errors

    try:
        execution_order = topological_sort(pf, target)
    except CycleError:
        return errors  # cycle errors are reported later

    for task_name in execution_order:
        task = pf.tasks[task_name]
        # Skip shell-only tasks — they don't invoke the LLM
        has_prompt = any(s.kind == "prompt" for s in task.steps)
        has_docker = task.docker is not None
        if not has_prompt and not has_docker:
            continue

        # Check per-task LLM dispatcher if configured
        provider = pf.get_llm_for_task(task_name)
        if provider:
            per_task_dispatcher = _dispatcher_for_provider(provider)
            _check(per_task_dispatcher, f"task '{task_name}'")

    return errors


def _resolve_target(pf: Promptfile, target: str | None) -> str | None:
    if target is None:
        return pf.default_task
    return pf.resolve_alias(target)


def _build_task_args(pf: Promptfile, target: str | None, raw_args: list[str]) -> tuple[dict[str, str] | None, str | None]:
    """Build task argument mapping for the selected target."""
    target = _resolve_target(pf, target)
    if not target or target not in pf.tasks or not pf.tasks[target].arguments:
        return None, None

    task_def = pf.tasks[target]
    task_args: dict[str, str] = {}
    for idx, arg_def in enumerate(task_def.arguments):
        if arg_def.variadic:
            remaining = raw_args[idx:]
            if arg_def.variadic == "+" and not remaining:
                return None, f"task {target!r} requires at least one value for +{arg_def.name}"
            task_args[arg_def.name] = " ".join(remaining)
            break
        if idx < len(raw_args):
            task_args[arg_def.name] = raw_args[idx]
        elif arg_def.default is not None:
            task_args[arg_def.name] = arg_def.default
        else:
            return None, f"task {target!r} requires argument {arg_def.name!r}"
    return task_args, None


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
    if task.options.when:
        options.append(f"when={'; '.join(task.options.when)}")
    if task.options.ssh_identity:
        options.append(f"ssh-key={task.options.ssh_identity}")
    if task.options.ssh_strict_host_key_checking:
        options.append(f"ssh-strict-host-key-checking={task.options.ssh_strict_host_key_checking}")
    if task.options.secrets:
        options.append(f"secrets={task.options.secrets}")
    return options


def _plan_payload(
    pf: Promptfile,
    target: str,
    task_args: dict[str, str] | None,
    promptfile_path: str,
) -> dict[str, object]:
    """Return an execution plan without running tasks."""
    order = topological_sort(pf, target)
    tasks: list[dict[str, object]] = []
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
            steps.append({"kind": step.kind, "content": content})

        tasks.append({
            "index": idx,
            "name": task_name,
            "provider": _provider_label(pf, task_name),
            "hosts": host_payload,
            "dependencies": list(task.dependencies),
            "options": _task_plan_options(task),
            "steps": steps,
        })

    return {
        "target": target,
        "variables": {key: pf.variables[key] for key in sorted(pf.variables)},
        "execution_order": order,
        "tasks": tasks,
    }


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_plan(pf: Promptfile, target: str, task_args: dict[str, str] | None, promptfile_path: str) -> None:
    """Print an execution plan without running tasks."""
    payload = _plan_payload(pf, target, task_args, promptfile_path)
    order = payload["execution_order"]
    print(f"Plan for {target}")
    print()
    print("Variables:")
    if pf.variables:
        for key in sorted(pf.variables):
            print(f"  {key}={pf.variables[key]!r}")
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
        if options:
            print(f"     options: {', '.join(options)}")
        print("     steps:")
        for step in pf.resolve_steps(
            task_name,
            args,
            promptfile_path=promptfile_path,
            mask_secrets=True,
        ):
            if step.kind == "shell":
                print(f"       ! {step.content}")
            elif step.kind == "echo":
                print(f"       @echo {step.content}")
            else:
                first_line = step.content.splitlines()[0] if step.content else ""
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


def _validate_safe_mode(
    pf: Promptfile,
    target: str,
    *,
    allow_shell: bool,
    allow_ssh: bool,
    allow_docker: bool,
    allow_llm: bool,
) -> list[str]:
    """Return safe-mode violations for the target execution subgraph."""
    errors: list[str] = []
    for task_name in topological_sort(pf, target):
        task = pf.tasks[task_name]
        has_shell = any(step.kind == "shell" for step in task.steps)
        has_prompt = any(step.kind == "prompt" for step in task.steps)
        has_ssh = has_shell and bool(task.options.on)

        if task.docker and not allow_docker:
            errors.append(f"task {task_name!r} uses a docker block; pass --allow-docker")
        if has_ssh and not allow_ssh:
            errors.append(f"task {task_name!r} runs shell steps over SSH; pass --allow-ssh")
        if has_shell and not has_ssh and not allow_shell:
            errors.append(f"task {task_name!r} runs local shell steps; pass --allow-shell")
        if has_prompt and not allow_llm:
            errors.append(f"task {task_name!r} sends prompts to an LLM; pass --allow-llm")
    return errors


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


def _task_references_secret(pf: Promptfile, task_name: str) -> bool:
    """Return True if a task/guidance/agent includes secret placeholders."""
    task = pf.tasks[task_name]
    if any("{{#secret:" in step.content for step in task.steps):
        return True
    agent = pf.get_agent_for_task(task_name)
    if agent and "{{#secret:" in agent.instructions:
        return True
    return bool(pf.guidance and "{{#secret:" in pf.guidance)


def _secret_refs_for_task(pf: Promptfile, task_name: str) -> list[str]:
    """Return secret reference names used by a task."""
    task = pf.tasks[task_name]
    texts = [step.content for step in task.steps]
    agent = pf.get_agent_for_task(task_name)
    if agent:
        texts.append(agent.instructions)
    if pf.guidance:
        texts.append(pf.guidance)
    refs: list[str] = []
    for text in texts:
        refs.extend(match.strip() for match in re.findall(r"\{\{#secret:(.+?)\}\}", text))
    return refs


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

        if task.options.llm and task.options.llm not in pf.llm_providers:
            error(
                "unknown-provider",
                f"task {task_name!r} references unknown LLM provider {task.options.llm!r}",
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
            provider_name = _provider_for_check(pf, task_name)
            if provider_name and provider_name in pf.llm_providers:
                used_providers.add(provider_name)
            elif not provider_name:
                fallback_claude_needed = True

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
            warn("docker-generation", f"task {task_name!r} generates/builds Docker artifacts", task=task_name)

        sandbox = task.options.sandbox or pf.settings.sandbox
        if sandbox and sandbox != "none" and has_shell:
            used_sandboxes.add(sandbox)
        if task.options.sandbox_net == "host":
            warn("sandbox-host-network", f"task {task_name!r} uses sandbox-net=host", task=task_name)

        if _task_references_secret(pf, task_name):
            backend = task.options.secrets or pf.settings.secrets or "env"
            used_secret_backends.setdefault(backend, set()).update(_secret_refs_for_task(pf, task_name))

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
            error("missing-sandbox-tool", f"{tool!r} required for sandbox {sandbox!r} was not found on PATH")

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
            error("missing-secrets-tool", f"{tool!r} required for secrets backend {backend!r} was not found on PATH")
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


def _print_check_result(payload: dict[str, object]) -> None:
    """Print check results in a concise human-readable format."""
    for issue in payload["errors"]:
        print(f"error[{issue['code']}]: {issue['message']}")
    for issue in payload["warnings"]:
        print(f"warning[{issue['code']}]: {issue['message']}")
    summary = payload["summary"]
    if payload["ok"]:
        print(
            f"OK: {summary['tasks']} task(s), "
            f"{summary['warnings']} warning(s)"
        )
    else:
        print(
            f"FAILED: {summary['errors']} error(s), "
            f"{summary['warnings']} warning(s)"
        )


def _history_payload(limit_raw: str | None = None) -> dict[str, object]:
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
        print(
            f"{row['id']:>4}  {status:<6}  {row['target']:<20} "
            f"{row['duration_ms']}ms  {row['started_at']}"
        )


def _serve(pf: Promptfile, dispatcher: Dispatcher, promptfile_path: str, bind: str) -> int:
    """Run a small local HTTP UI/API for self-hosted use."""
    host, _, port_raw = bind.partition(":")
    host = host or "127.0.0.1"
    port = int(port_raw or "8765")

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path == "/api/tasks":
                self._json(200, [
                    {
                        "name": name,
                        "private": pf.tasks[name].options.private,
                        "doc": pf.tasks[name].options.doc or (pf.tasks[name].prompt.splitlines() or [""])[0],
                    }
                    for name in pf.task_order
                    if not pf.tasks[name].options.private
                ])
                return
            if parsed.path == "/api/history":
                self._json(200, list_runs(20))
                return
            if parsed.path not in ("/", "/index.html"):
                self._json(404, {"error": "not found"})
                return
            rows = "".join(
                f"<li><form method='post' action='/api/run?task={name}'>"
                f"<button type='submit'>{name}</button> "
                f"{pf.tasks[name].options.doc or (pf.tasks[name].prompt.splitlines() or [''])[0]}"
                "</form></li>"
                for name in pf.task_order
                if not pf.tasks[name].options.private
            )
            body = (
                "<!doctype html><title>makethlm</title>"
                "<h1>makethlm</h1><h2>Tasks</h2><ul>"
                + rows
                + "</ul><p>API: <code>/api/tasks</code>, <code>/api/history</code>, "
                "<code>POST /api/run?task=name</code></p>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path != "/api/run":
                self._json(404, {"error": "not found"})
                return
            task = parse_qs(parsed.query).get("task", [None])[0]
            if not task:
                self._json(400, {"error": "missing task"})
                return
            runner = Runner(pf, dispatcher, quiet=True, verbose=False, promptfile_path=promptfile_path)
            started = time.monotonic()
            try:
                result = runner.run(task)
                duration_ms = int((time.monotonic() - started) * 1000)
                run_id = record_run(result, duration_ms=duration_ms, promptfile_path=promptfile_path)
                self._json(200 if result.success else 500, {
                    "id": run_id,
                    "target": result.target,
                    "success": result.success,
                    "tasks": [
                        {"task": tr.task_name, "success": tr.success, "response": tr.response}
                        for tr in result.task_results
                    ],
                })
            except Exception as e:  # pragma: no cover - defensive server boundary
                self._json(500, {"error": str(e)})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((host, port), Handler)
    print(f"Serving makethlm on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        return 130
    return 0


def _step_result_payload(step: StepResult) -> dict[str, object]:
    """Return a JSON-safe step result payload."""
    return {
        "kind": step.kind,
        "content": step.content,
        "response": step.response,
        "success": step.success,
        "exit_code": 0 if step.success else 1,
        "host": step.host,
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
    if run_id is not None:
        payload["run_id"] = run_id
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    if args.jobs is not None and args.jobs < 1:
        print("error: --jobs must be at least 1", file=sys.stderr)
        return 1
    if args.jobs is not None:
        args.parallel = True

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
        allow_backticks = (not args.safe and not args.check) or args.allow_backticks
        pf = parse(source, filename=str(pf_path), allow_backticks=allow_backticks)
    except ParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Apply variable overrides from CLI
    for var_str in args.var:
        if "=" not in var_str:
            print(f"error: invalid --var format (expected name=value): {var_str!r}", file=sys.stderr)
            return 1
        key, value = var_str.split("=", 1)
        pf.variables[key.strip()] = value.strip()

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
        for k, v in pf.variables.items():
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
                parts = []
                for a in task.arguments:
                    prefix = a.variadic or ""
                    if a.default is not None:
                        parts.append(f'{prefix}{a.name}="{a.default}"')
                    else:
                        parts.append(f"{prefix}{a.name}")
                args_str = f"({', '.join(parts)})"
            print(f"  {name}{args_str}{deps_str}{flag_str}")
        return 0

    # List mode
    if args.list_tasks:
        # Collect tasks by group, filtering out private tasks
        ungrouped: list[str] = []
        groups: dict[str, list[str]] = {}
        for name in pf.task_order:
            task = pf.tasks[name]
            if task.options.private:
                continue
            group = task.options.group
            if group:
                groups.setdefault(group, []).append(name)
            else:
                ungrouped.append(name)

        def _print_task(name: str) -> None:
            task = pf.tasks[name]
            parts: list[str] = []
            if task.dependencies:
                parts.append(f"depends: {', '.join(task.dependencies)}")
            if task.arguments:
                arg_strs = []
                for a in task.arguments:
                    prefix = a.variadic or ""
                    if a.default is not None:
                        arg_strs.append(f'{prefix}{a.name}="{a.default}"')
                    else:
                        arg_strs.append(f"{prefix}{a.name}")
                parts.append(f"args: {', '.join(arg_strs)}")
            if task.docker:
                parts.append(f"docker:{task.docker.tag}")
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
            suffix = f" ({'; '.join(parts)})" if parts else ""
            doc = task.options.doc
            if doc:
                desc = doc
            else:
                desc = task.prompt.split("\n")[0]
            if len(desc) > 60:
                desc = desc[:57] + "..."
            print(f"  {name}{suffix}")
            print(f"    {desc}")

        for name in ungrouped:
            _print_task(name)

        for group_name, task_names in groups.items():
            print()
            print(f"  [{group_name}]")
            for name in task_names:
                _print_task(name)

        # List aliases
        if pf.aliases:
            print()
            print("  aliases:")
            for alias_name, alias_target in pf.aliases.items():
                print(f"    {alias_name} -> {alias_target}")

        # List functions
        if pf.functions:
            print()
            print("  functions:")
            for fn_name, fn in pf.functions.items():
                first_line = fn.body.split("\n")[0]
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                print(f"    {fn_name}: {first_line}")

        # List LLM providers
        if pf.llm_providers:
            print()
            default = pf.default_llm
            print("  llm providers:")
            for pname, prov in pf.llm_providers.items():
                marker = " (default)" if pname == default else ""
                model_str = f" model={prov.model}" if prov.model else ""
                print(f"    {pname}{model_str}{marker}")

        # List host groups
        if pf.host_groups:
            print()
            print("  host groups:")
            for gname, group in pf.host_groups.items():
                user_str = f" user={group.user}" if group.user else ""
                port_str = f" port={group.port}" if group.port else ""
                print(f"    {gname}{user_str}{port_str}: {', '.join(group.hosts)}")

        # List guidance
        if pf.guidance:
            print()
            first_line = pf.guidance.split("\n")[0]
            if len(first_line) > 60:
                first_line = first_line[:57] + "..."
            print(f"  guidance: {first_line}")

        # List agents
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

        return 0

    target = _resolve_target(pf, args.task)

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

    # Build dispatcher
    if args.dry_run:
        dispatcher = DryRunDispatcher()
    elif args.shell:
        dispatcher = ShellDispatcher(args.shell)
    elif args.codex:
        dispatcher = CodexDispatcher(model=args.model)
    else:
        dispatcher = ClaudeDispatcher(model=args.model)

    if args.serve:
        return _serve(pf, dispatcher, str(pf_path), args.serve)

    # Validate that required CLI tools are installed
    tool_errors = _validate_tools(dispatcher, pf, args.task)
    if tool_errors:
        for err in tool_errors:
            print(err, file=sys.stderr)
        return 1

    if args.safe and not args.dry_run:
        if target is None:
            print("error: no tasks defined in Promptfile", file=sys.stderr)
            return 1
        if target not in pf.tasks:
            print(f"error: unknown task: {target!r}", file=sys.stderr)
            return 1
        try:
            safe_errors = _validate_safe_mode(
                pf,
                target,
                allow_shell=args.allow_shell,
                allow_ssh=args.allow_ssh,
                allow_docker=args.allow_docker,
                allow_llm=args.allow_llm,
            )
        except CycleError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if safe_errors:
            for err in safe_errors:
                print(f"error: safe mode blocked execution: {err}", file=sys.stderr)
            return 1

    # Run
    runner = Runner(
        pf,
        dispatcher,
        quiet=args.quiet,
        verbose=not args.dry_run and not args.json_output,
        promptfile_path=str(pf_path),
        dry_run=args.dry_run,
    )
    try:
        started = time.monotonic()
        if args.parallel:
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
        run_id = record_run(result, duration_ms=duration_ms, promptfile_path=str(pf_path))

    if args.json_output:
        _print_json(_run_result_payload(
            result,
            duration_ms=duration_ms,
            promptfile_path=str(pf_path),
            dry_run=args.dry_run,
            parallel=args.parallel,
            jobs=args.jobs,
            run_id=run_id,
        ))
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

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
