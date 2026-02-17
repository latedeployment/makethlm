"""Task runner with dependency resolution, SSH execution, and LLM routing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import HostGroup, LLMProvider, Promptfile, Task, TaskStep, evaluate_condition
from .dispatcher import Dispatcher, DispatchResult, ClaudeDispatcher, ShellDispatcher
from .subprocess_util import run_subprocess as _run_subprocess


def _log(msg: str, *, bold: bool = False, dim: bool = False) -> None:
    """Print a progress message to stderr."""
    if sys.stderr.isatty():
        if bold:
            msg = f"\033[1m{msg}\033[0m"
        elif dim:
            msg = f"\033[2m{msg}\033[0m"
    print(msg, file=sys.stderr, flush=True)


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.1f}s"
    return f"{seconds:.1f}s"


class CycleError(Exception):
    """Raised when task dependencies contain a cycle."""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"dependency cycle detected: {' -> '.join(cycle)}")


def topological_sort(pf: Promptfile, target: str) -> list[str]:
    """Return the tasks needed to run `target` in dependency order."""
    order: list[str] = []
    visited: set[str] = set()
    in_stack: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in in_stack:
            cycle_start = path.index(name)
            raise CycleError(path[cycle_start:] + [name])
        if name in visited:
            return
        in_stack.add(name)
        path.append(name)
        for dep in pf.tasks[name].dependencies:
            visit(dep, path)
        path.pop()
        in_stack.remove(name)
        visited.add(name)
        order.append(name)

    visit(target, [])
    return order


def topological_levels(pf: Promptfile, target: str) -> list[list[str]]:
    """Return tasks grouped into levels for parallel execution.

    Tasks at the same level have no dependencies on each other and can
    be executed concurrently.
    """
    order = topological_sort(pf, target)

    # Build the in-degree map restricted to our subgraph
    order_set = set(order)
    completed: set[str] = set()
    remaining = list(order)
    levels: list[list[str]] = []

    while remaining:
        # A task is ready if all its deps (within our subgraph) are completed
        level: list[str] = []
        for name in remaining:
            deps = [d for d in pf.tasks[name].dependencies if d in order_set]
            if all(d in completed for d in deps):
                level.append(name)

        if not level:
            # Should never happen if topological_sort is correct, but safety valve
            break

        levels.append(level)
        for name in level:
            completed.add(name)
        remaining = [n for n in remaining if n not in completed]

    return levels


def _parse_cache_duration(duration: str) -> float:
    """Parse a cache duration string like '1h', '30m', '1d' into seconds."""
    m = re.match(r'^(\d+)\s*([smhd])$', duration.strip())
    if not m:
        raise ValueError(f"invalid cache duration: {duration!r} (expected e.g. '1h', '30m', '1d')")
    value = int(m.group(1))
    unit = m.group(2)
    multipliers = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
    return value * multipliers[unit]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Result of running a single step within a task."""

    kind: str  # "shell", "prompt", "echo", "docker-generate", "docker-build", "ssh"
    content: str
    response: str
    success: bool
    host: str | None = None  # set for SSH-executed steps


@dataclass
class TaskResult:
    """Result of running a single task."""

    task_name: str
    prompt_sent: str
    response: str
    success: bool
    step_results: list[StepResult] = field(default_factory=list)


@dataclass
class RunResult:
    """Result of running a target (including all dependencies)."""

    target: str
    task_results: list[TaskResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(r.success for r in self.task_results)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_DOCKER_GENERATE_PREFIX = (
    "Generate a Dockerfile based on the following description. "
    "Output ONLY the raw Dockerfile content — no markdown fences, "
    "no explanation, no commentary. Just the Dockerfile.\n\n"
)


def _build_ssh_command(host: str, command: str, group: HostGroup) -> str:
    """Build an SSH command string for remote execution."""
    parts = ["ssh"]
    if group.port:
        parts.extend(["-p", str(group.port)])
    parts.append("-o")
    parts.append("BatchMode=yes")
    target = f"{group.user}@{host}" if group.user else host
    parts.append(target)
    parts.append(command)
    return " ".join(parts)


def _dispatcher_for_provider(provider: LLMProvider) -> Dispatcher:
    """Create a Dispatcher from an LLMProvider configuration."""
    if provider.shell_template:
        return ShellDispatcher(provider.shell_template)
    # Default: use Claude CLI dispatcher
    return ClaudeDispatcher(model=provider.model)


def _load_dotenv(path: str | None = None, required: bool = False) -> None:
    """Load a .env file into os.environ (simple key=value parser).

    The *path* supports environment variable expansion (``$HOME``, ``${VAR}``)
    and user home expansion (``~``).
    """
    if path:
        env_path = Path(os.path.expandvars(os.path.expanduser(path)))
    else:
        env_path = Path(".env")
    if not env_path.is_file():
        if required:
            raise FileNotFoundError(f".env file not found: {env_path}")
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ.setdefault(key, value)


class Runner:
    """Executes tasks from a Promptfile using a Dispatcher."""

    def __init__(
        self,
        pf: Promptfile,
        dispatcher: Dispatcher,
        *,
        quiet: bool = False,
        verbose: bool = True,
        promptfile_path: str | None = None,
    ):
        self.pf = pf
        self.dispatcher = dispatcher  # fallback/default dispatcher
        self._dotenv_loaded = False
        self._exports_applied = False
        self.quiet = quiet  # global quiet from CLI or settings
        self.verbose = verbose and not quiet  # show progress unless quiet
        self.promptfile_path = promptfile_path
        self.artifacts: dict[str, dict[str, str]] = {}  # task_name -> {stdout, stderr, exit_code, success, response}
        self._cache_dir = Path(os.path.expanduser("~/.cache/makethlm"))

    def _cache_key(self, task: Task) -> str:
        """Generate a cache key for a task based on its name and inputs."""
        h = hashlib.sha256()
        h.update(task.name.encode())
        # Include step content in the hash
        for step in task.steps:
            h.update(step.content.encode())
        # Include resolved variables used by this task
        return h.hexdigest()[:16]

    def _get_cached_result(self, task: Task) -> TaskResult | None:
        """Return a cached TaskResult if valid, else None."""
        if not task.options.cache:
            return None
        try:
            duration = _parse_cache_duration(task.options.cache)
        except ValueError:
            return None

        cache_file = self._cache_dir / f"{task.name}_{self._cache_key(task)}.json"
        if not cache_file.exists():
            return None

        try:
            data = json.loads(cache_file.read_text())
            cached_at = data.get("cached_at", 0)
            if time.time() - cached_at > duration:
                cache_file.unlink(missing_ok=True)
                return None
            return TaskResult(
                task_name=data["task_name"],
                prompt_sent=data.get("prompt_sent", ""),
                response=data.get("response", ""),
                success=data.get("success", True),
            )
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def _save_cached_result(self, task: Task, result: TaskResult) -> None:
        """Save a task result to the cache."""
        if not task.options.cache:
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._cache_dir / f"{task.name}_{self._cache_key(task)}.json"
            data = {
                "task_name": result.task_name,
                "prompt_sent": result.prompt_sent,
                "response": result.response,
                "success": result.success,
                "cached_at": time.time(),
            }
            cache_file.write_text(json.dumps(data))
        except OSError:
            pass

    def _get_dispatcher(self, task: Task) -> Dispatcher:
        """Return the appropriate dispatcher for a task.

        Priority: agent LLM/model > per-task LLM > default LLM > fallback.
        """
        agent = self.pf.get_agent_for_task(task.name)
        provider = self.pf.get_llm_for_task(task.name)

        # Agent-level LLM override
        if agent and agent.llm and agent.llm in self.pf.llm_providers:
            agent_provider = self.pf.llm_providers[agent.llm]
            # Agent model overrides the provider's model
            if agent.model:
                agent_provider = LLMProvider(
                    name=agent_provider.name,
                    model=agent.model,
                    api_key=agent_provider.api_key,
                    base_url=agent_provider.base_url,
                    shell_template=agent_provider.shell_template,
                )
            return _dispatcher_for_provider(agent_provider)

        # Agent model override (without agent-level LLM)
        if agent and agent.model and provider:
            provider = LLMProvider(
                name=provider.name,
                model=agent.model,
                api_key=provider.api_key,
                base_url=provider.base_url,
                shell_template=provider.shell_template,
            )
            return _dispatcher_for_provider(provider)

        if provider:
            return _dispatcher_for_provider(provider)
        return self.dispatcher

    def _ensure_dotenv(self) -> None:
        """Load .env if ``set dotenv-load`` is enabled and not yet loaded."""
        if not self._dotenv_loaded and self.pf.settings.dotenv_load:
            _load_dotenv(
                path=self.pf.settings.dotenv_path,
                required=self.pf.settings.dotenv_required,
            )
            self._dotenv_loaded = True

    def _apply_exports(self) -> None:
        """Export variables to os.environ."""
        if self._exports_applied:
            return
        self._exports_applied = True
        exported = self.pf.get_exported_env()
        for key, value in exported.items():
            os.environ.setdefault(key, value)

    def _resolve_working_dir(self, task: Task) -> str | None:
        """Return effective working directory for a task."""
        if task.options.no_cd:
            return None
        return task.options.working_dir or self.pf.settings.working_dir

    def _should_echo(self, task: Task, step: TaskStep) -> bool:
        """Return True if a command should be echoed before execution."""
        if step.quiet:
            return False
        if task.options.no_quiet:
            return True
        if self.quiet or self.pf.settings.quiet:
            return False
        return True

    def _prompt_confirm(self, task: Task) -> bool:
        """Prompt user for confirmation if [confirm] is set. Returns True to proceed."""
        confirm = task.options.confirm
        if not confirm:
            return True
        if isinstance(confirm, str) and confirm not in ("True", "true"):
            message = confirm
        else:
            message = f"Run task {task.name!r}?"
        try:
            answer = input(f"{message} [y/N] ")
            return answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def run(self, target: str | None = None, args: dict[str, str] | None = None) -> RunResult:
        """Run a target task and all its dependencies."""
        self._ensure_dotenv()
        self._apply_exports()

        if target is None:
            target = self.pf.default_task
            if target is None:
                raise ValueError("no tasks defined in Promptfile")

        # Resolve aliases
        target = self.pf.resolve_alias(target)

        if target not in self.pf.tasks:
            raise KeyError(f"unknown task: {target!r}")

        execution_order = topological_sort(self.pf, target)
        result = RunResult(target=target)

        total = len(execution_order)
        if self.verbose and total > 1:
            dep_names = [n for n in execution_order if n != target]
            _log(f"Running {target} ({total} tasks: {', '.join(dep_names)} -> {target})", bold=True)

        for idx, task_name in enumerate(execution_order, 1):
            task = self.pf.tasks[task_name]

            # OS filter: skip tasks not meant for this OS
            if task.options.should_skip_for_os():
                if self.verbose:
                    _log(f"  [{idx}/{total}] {task_name} ... skipped (requires {task.options.os_filter})", dim=True)
                result.task_results.append(TaskResult(
                    task_name=task_name,
                    prompt_sent="",
                    response=f"[skipped] not applicable on this OS (requires {task.options.os_filter})",
                    success=True,
                ))
                continue

            # When conditions: evaluate before running
            if task.options.when:
                context = self.pf._build_context(task_name, promptfile_path=self.promptfile_path)
                # Also include artifact variables
                for art_name, art_data in self.artifacts.items():
                    for key, value in art_data.items():
                        context[f"{art_name}.{key}"] = value
                all_met = all(evaluate_condition(cond, context) for cond in task.options.when)
                if not all_met:
                    if self.verbose:
                        _log(f"  [{idx}/{total}] {task_name} ... skipped (when condition not met)", dim=True)
                    result.task_results.append(TaskResult(
                        task_name=task_name,
                        prompt_sent="",
                        response="[skipped] when condition not met",
                        success=True,
                    ))
                    # Still store a "skipped" artifact
                    self.artifacts[task_name] = {
                        "stdout": "",
                        "stderr": "",
                        "exit_code": "0",
                        "success": "skipped",
                        "response": "",
                    }
                    continue

            # Confirm prompt (only for the explicitly targeted task)
            if task_name == target and task.options.confirm:
                if not self._prompt_confirm(task):
                    result.task_results.append(TaskResult(
                        task_name=task_name,
                        prompt_sent="",
                        response="[skipped] user declined confirmation",
                        success=True,
                    ))
                    return result

            # Check cache
            cached = self._get_cached_result(task)
            if cached is not None:
                if self.verbose:
                    _log(f"  [{idx}/{total}] {task_name} ... cached", dim=True)
                result.task_results.append(cached)
                self._store_artifact(task, cached)
                artifact_name = task.options.register or task.name
                if artifact_name in self.artifacts:
                    art = self.artifacts[artifact_name]
                    for key, value in art.items():
                        self.pf.variables[f"{artifact_name}.{key}"] = value
                continue

            if self.verbose:
                step_count = len(task.steps)
                shell_count = sum(1 for s in task.steps if s.kind == "shell")
                prompt_count = step_count - shell_count
                parts = []
                if shell_count:
                    parts.append(f"{shell_count} shell")
                if prompt_count:
                    parts.append(f"{prompt_count} prompt")
                step_desc = f" ({', '.join(parts)})" if parts else ""
                _log(f"  [{idx}/{total}] {task_name}{step_desc} ...", bold=True)

            task_args = args if task_name == target else None
            t0 = time.monotonic()
            task_result = self._run_task(task, task_args)
            elapsed = time.monotonic() - t0
            result.task_results.append(task_result)

            # Save to cache if configured
            self._save_cached_result(task, task_result)

            # Store artifact
            self._store_artifact(task, task_result)

            # Inject artifact variables into the Promptfile's variable context
            # so downstream tasks can access them
            artifact_name = task.options.register or task.name
            if artifact_name in self.artifacts:
                art = self.artifacts[artifact_name]
                for key, value in art.items():
                    self.pf.variables[f"{artifact_name}.{key}"] = value

            # Fire webhook if configured
            self._fire_webhook(task, task_result, elapsed)

            if self.verbose:
                status = "done" if task_result.success else "FAILED"
                _log(f"  [{idx}/{total}] {task_name} {status} ({_fmt_elapsed(elapsed)})")

            if not task_result.success:
                break

        return result

    def run_parallel(self, target: str | None = None, args: dict[str, str] | None = None) -> RunResult:
        """Run a target task with independent tasks at each level in parallel.

        Uses asyncio.gather to run tasks that share no dependencies concurrently.
        """
        self._ensure_dotenv()
        self._apply_exports()

        if target is None:
            target = self.pf.default_task
            if target is None:
                raise ValueError("no tasks defined in Promptfile")

        target = self.pf.resolve_alias(target)
        if target not in self.pf.tasks:
            raise KeyError(f"unknown task: {target!r}")

        levels = topological_levels(self.pf, target)
        result = RunResult(target=target)

        total = sum(len(level) for level in levels)
        if self.verbose and total > 1:
            flat = [n for level in levels for n in level if n != target]
            _log(f"Running {target} ({total} tasks, {len(levels)} levels)", bold=True)

        idx = 0
        for level in levels:
            if len(level) == 1:
                # Single task — run synchronously (no asyncio overhead)
                idx += 1
                task_name = level[0]
                task_result = self._run_single_task_in_pipeline(
                    task_name, target, args, idx, total, result,
                )
                if task_result is not None and not task_result.success:
                    break
            else:
                # Multiple tasks — run in parallel
                level_results = asyncio.run(
                    self._run_level_parallel(level, target, args, idx, total, result)
                )
                idx += len(level)
                failed = any(not tr.success for tr in level_results if tr is not None)
                if failed:
                    break

        return result

    def _run_single_task_in_pipeline(
        self,
        task_name: str,
        target: str,
        args: dict[str, str] | None,
        idx: int,
        total: int,
        result: RunResult,
    ) -> TaskResult | None:
        """Run a single task within the pipeline, handling skip/cache/confirm logic.

        Returns the TaskResult, or None if skipped.
        """
        task = self.pf.tasks[task_name]

        # OS filter
        if task.options.should_skip_for_os():
            if self.verbose:
                _log(f"  [{idx}/{total}] {task_name} ... skipped (requires {task.options.os_filter})", dim=True)
            tr = TaskResult(task_name=task_name, prompt_sent="",
                            response=f"[skipped] not applicable on this OS", success=True)
            result.task_results.append(tr)
            return tr

        # When conditions
        if task.options.when:
            context = self.pf._build_context(task_name, promptfile_path=self.promptfile_path)
            for art_name, art_data in self.artifacts.items():
                for key, value in art_data.items():
                    context[f"{art_name}.{key}"] = value
            if not all(evaluate_condition(cond, context) for cond in task.options.when):
                if self.verbose:
                    _log(f"  [{idx}/{total}] {task_name} ... skipped (when condition not met)", dim=True)
                tr = TaskResult(task_name=task_name, prompt_sent="",
                                response="[skipped] when condition not met", success=True)
                result.task_results.append(tr)
                self.artifacts[task_name] = {"stdout": "", "stderr": "", "exit_code": "0", "success": "skipped", "response": ""}
                return tr

        # Cache check
        cached = self._get_cached_result(task)
        if cached is not None:
            if self.verbose:
                _log(f"  [{idx}/{total}] {task_name} ... cached", dim=True)
            result.task_results.append(cached)
            self._store_artifact(task, cached)
            artifact_name = task.options.register or task.name
            if artifact_name in self.artifacts:
                for key, value in self.artifacts[artifact_name].items():
                    self.pf.variables[f"{artifact_name}.{key}"] = value
            return cached

        # Confirm
        if task_name == target and task.options.confirm:
            if not self._prompt_confirm(task):
                tr = TaskResult(task_name=task_name, prompt_sent="",
                                response="[skipped] user declined confirmation", success=True)
                result.task_results.append(tr)
                return tr

        if self.verbose:
            step_count = len(task.steps)
            shell_count = sum(1 for s in task.steps if s.kind == "shell")
            prompt_count = step_count - shell_count
            parts = []
            if shell_count:
                parts.append(f"{shell_count} shell")
            if prompt_count:
                parts.append(f"{prompt_count} prompt")
            step_desc = f" ({', '.join(parts)})" if parts else ""
            _log(f"  [{idx}/{total}] {task_name}{step_desc} ...", bold=True)

        task_args = args if task_name == target else None
        t0 = time.monotonic()
        task_result = self._run_task(task, task_args)
        elapsed = time.monotonic() - t0
        result.task_results.append(task_result)

        self._save_cached_result(task, task_result)
        self._store_artifact(task, task_result)
        artifact_name = task.options.register or task.name
        if artifact_name in self.artifacts:
            for key, value in self.artifacts[artifact_name].items():
                self.pf.variables[f"{artifact_name}.{key}"] = value
        self._fire_webhook(task, task_result, elapsed)

        if self.verbose:
            status = "done" if task_result.success else "FAILED"
            _log(f"  [{idx}/{total}] {task_name} {status} ({_fmt_elapsed(elapsed)})")

        return task_result

    async def _run_level_parallel(
        self,
        level: list[str],
        target: str,
        args: dict[str, str] | None,
        start_idx: int,
        total: int,
        result: RunResult,
    ) -> list[TaskResult | None]:
        """Run all tasks in a level concurrently using asyncio."""
        loop = asyncio.get_event_loop()
        tasks = []
        for i, task_name in enumerate(level):
            idx = start_idx + i + 1
            tasks.append(
                loop.run_in_executor(
                    None,
                    self._run_single_task_in_pipeline,
                    task_name, target, args, idx, total, result,
                )
            )
        return await asyncio.gather(*tasks)

    def _run_task(self, task: Task, args: dict[str, str] | None) -> TaskResult:
        """Execute a single task's steps."""
        resolved_steps = self.pf.resolve_steps(task.name, args, promptfile_path=self.promptfile_path)
        prompt_sent = self.pf.resolve_prompt(task.name, args, promptfile_path=self.promptfile_path)

        if task.docker:
            return self._run_docker_task(task, resolved_steps, prompt_sent)

        host_group = self.pf.get_hosts_for_task(task.name)
        if host_group:
            return self._run_on_hosts(task, resolved_steps, prompt_sent, host_group)

        return self._run_local(task, resolved_steps, prompt_sent)

    def _run_local(
        self, task: Task, resolved_steps: list[TaskStep], prompt_sent: str
    ) -> TaskResult:
        """Run steps locally."""
        step_results: list[StepResult] = []
        all_responses: list[str] = []
        success = True
        dispatcher = self._get_dispatcher(task)
        working_dir = self._resolve_working_dir(task)

        for step in resolved_steps:
            if step.kind == "echo":
                _log(f"         {step.content}")
                sr = StepResult(kind="echo", content=step.content, response="", success=True)
            elif step.kind == "shell":
                if self.verbose:
                    cmd_preview = step.content if len(step.content) <= 60 else step.content[:57] + "..."
                    _log(f"         $ {cmd_preview}", dim=True)
                sr = self._run_shell_step(step, task=task, working_dir=working_dir)
            else:
                if self.verbose:
                    prompt_preview = step.content.split("\n")[0]
                    if len(prompt_preview) > 60:
                        prompt_preview = prompt_preview[:57] + "..."
                    _log(f"         > sending prompt to LLM ...", dim=True)
                sr = self._run_prompt_step(step, task, dispatcher)

            step_results.append(sr)
            all_responses.append(sr.response)

            if not sr.success:
                success = False
                if not task.options.no_exit_message:
                    pass  # normal error reporting
                break

        return TaskResult(
            task_name=task.name,
            prompt_sent=prompt_sent,
            response="\n".join(all_responses),
            success=success,
            step_results=step_results,
        )

    def _run_on_hosts(
        self,
        task: Task,
        resolved_steps: list[TaskStep],
        prompt_sent: str,
        host_group: HostGroup,
    ) -> TaskResult:
        """Run shell steps on each host via SSH. Prompt steps run locally."""
        step_results: list[StepResult] = []
        all_responses: list[str] = []
        success = True
        dispatcher = self._get_dispatcher(task)

        for step in resolved_steps:
            if step.kind == "echo":
                _log(f"         {step.content}")
                sr = StepResult(kind="echo", content=step.content, response="", success=True)
                step_results.append(sr)
                continue
            elif step.kind == "shell":
                # Execute on each host
                for host in host_group.hosts:
                    if self.verbose:
                        _log(f"         $ {step.content} (on {host})", dim=True)
                    sr = self._run_ssh_step(step, host, host_group)
                    step_results.append(sr)
                    all_responses.append(sr.response)
                    if not sr.success:
                        success = False
                        break
                if not success:
                    break
            else:
                # Prompt steps still run locally via LLM
                if self.verbose:
                    _log(f"         > sending prompt to LLM ...", dim=True)
                sr = self._run_prompt_step(step, task, dispatcher)
                step_results.append(sr)
                all_responses.append(sr.response)
                if not sr.success:
                    success = False
                    break

        return TaskResult(
            task_name=task.name,
            prompt_sent=prompt_sent,
            response="\n".join(all_responses),
            success=success,
            step_results=step_results,
        )

    def _sandbox_command(self, cmd: str, task: Task) -> str:
        """Wrap a shell command with sandbox isolation if configured.

        Returns the original command if no sandbox is active.
        """
        sandbox = task.options.sandbox or self.pf.settings.sandbox
        if not sandbox or sandbox == "none":
            return cmd

        if sandbox == "docker":
            image = task.options.sandbox_image or "ubuntu:latest"
            cwd = os.getcwd()
            parts = ["docker", "run", "--rm", "-v", f"{cwd}:/workspace", "-w", "/workspace"]
            if task.options.sandbox_mount:
                parts.extend(["-v", task.options.sandbox_mount])
            net = task.options.sandbox_net
            if net:
                parts.extend(["--net", net])
            parts.append(image)
            parts.extend(["sh", "-c", cmd])
            return " ".join(shlex.quote(p) for p in parts)

        elif sandbox == "systemd":
            parts = [
                "systemd-run", "--scope", "--quiet",
                "--property=PrivateTmp=yes",
                "--property=NoNewPrivileges=yes",
                "--property=ProtectSystem=strict",
                "sh", "-c", cmd,
            ]
            return " ".join(shlex.quote(p) for p in parts)

        elif sandbox == "bwrap":
            cwd = os.getcwd()
            parts = [
                "bwrap",
                "--ro-bind", "/", "/",
                "--dev", "/dev",
                "--tmpfs", "/tmp",
                "--bind", cwd, cwd,
                "sh", "-c", cmd,
            ]
            return " ".join(shlex.quote(p) for p in parts)

        return cmd

    def _run_shell_step(
        self,
        step: TaskStep,
        task: Task | None = None,
        working_dir: str | None = None,
    ) -> StepResult:
        """Execute a shell command locally."""
        shell_exe = self.pf.settings.shell or shutil.which("bash") or "/bin/bash"
        cmd = step.content

        # Strip inline comments if ignore-comments is set
        if self.pf.settings.ignore_comments and "#" in cmd:
            # Naive comment stripping — don't strip inside quotes
            in_single = False
            in_double = False
            for ci, ch in enumerate(cmd):
                if ch == "'" and not in_double:
                    in_single = not in_single
                elif ch == '"' and not in_single:
                    in_double = not in_double
                elif ch == "#" and not in_single and not in_double:
                    cmd = cmd[:ci].rstrip()
                    break

        # Apply sandbox wrapping if configured
        if task:
            cmd = self._sandbox_command(cmd, task)

        # Build environment with exported vars + makethlm defaults
        env = dict(os.environ)
        exported = self.pf.get_exported_env()
        if exported:
            env.update(exported)

        # Inject MAKETHLM_* env vars
        if task:
            env["MAKETHLM_TASK"] = task.name
        if self.promptfile_path:
            env["MAKETHLM_FILE"] = self.promptfile_path
            env["MAKETHLM_DIR"] = os.path.dirname(os.path.abspath(self.promptfile_path))
        env.setdefault("HOME", os.path.expanduser("~"))

        try:
            proc = _run_subprocess(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=working_dir,
                executable=shell_exe,
                env=env,
            )
            output = proc.stdout
            if proc.stderr:
                output += proc.stderr
            ok = proc.returncode == 0 or step.ignore_error
            return StepResult(
                kind="shell",
                content=step.content,
                response=output.strip() if not step.silent else "",
                success=ok,
            )
        except subprocess.TimeoutExpired:
            return StepResult(
                kind="shell",
                content=step.content,
                response="error: command timed out after 120s",
                success=step.ignore_error,
            )

    def _run_ssh_step(self, step: TaskStep, host: str, group: HostGroup) -> StepResult:
        """Execute a shell command on a remote host via SSH."""
        ssh_cmd = _build_ssh_command(host, step.content, group)
        try:
            proc = _run_subprocess(
                ssh_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = proc.stdout
            if proc.stderr:
                output += proc.stderr
            ok = proc.returncode == 0 or step.ignore_error
            return StepResult(
                kind="ssh",
                content=step.content,
                response=output.strip() if not step.silent else "",
                success=ok,
                host=host,
            )
        except subprocess.TimeoutExpired:
            return StepResult(
                kind="ssh",
                content=step.content,
                response=f"error: SSH to {host} timed out after 120s",
                success=step.ignore_error,
                host=host,
            )

    def _run_prompt_step(self, step: TaskStep, task: Task, dispatcher: Dispatcher) -> StepResult:
        """Send a prompt step to the LLM dispatcher."""
        dr = dispatcher.dispatch(step.content, task)
        return StepResult(
            kind="prompt",
            content=step.content,
            response=dr.response,
            success=dr.success,
        )

    def _run_docker_task(
        self,
        task: Task,
        resolved_steps: list[TaskStep],
        prompt_sent: str,
    ) -> TaskResult:
        """Handle a docker block: generate Dockerfile via LLM, then build."""
        docker = task.docker
        assert docker is not None
        step_results: list[StepResult] = []
        dispatcher = self._get_dispatcher(task)

        description = "\n".join(s.content for s in resolved_steps if s.kind == "prompt")
        generate_prompt = _DOCKER_GENERATE_PREFIX + description

        if self.verbose:
            _log(f"         > generating Dockerfile via LLM ...", dim=True)
        dr = dispatcher.dispatch(generate_prompt, task)
        step_results.append(StepResult(
            kind="docker-generate",
            content=generate_prompt,
            response=dr.response,
            success=dr.success,
        ))

        if not dr.success:
            return TaskResult(
                task_name=task.name,
                prompt_sent=prompt_sent,
                response=dr.response,
                success=False,
                step_results=step_results,
            )

        dockerfile_path = os.path.join(docker.context, docker.file)
        dockerfile_content = dr.response.strip()
        if dockerfile_content.startswith("```"):
            lines = dockerfile_content.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            dockerfile_content = "\n".join(lines)

        try:
            os.makedirs(os.path.dirname(dockerfile_path) or ".", exist_ok=True)
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_content + "\n")
        except OSError as e:
            step_results.append(StepResult(
                kind="docker-build",
                content=f"write {dockerfile_path}",
                response=f"error writing Dockerfile: {e}",
                success=False,
            ))
            return TaskResult(
                task_name=task.name,
                prompt_sent=prompt_sent,
                response=f"error writing Dockerfile: {e}",
                success=False,
                step_results=step_results,
            )

        tag = f"{task.name}:{docker.tag}"
        build_cmd = f"docker build -t {tag} -f {dockerfile_path} {docker.context}"
        build_step = TaskStep(kind="shell", content=build_cmd)
        build_result = self._run_shell_step(build_step)
        build_result.kind = "docker-build"
        step_results.append(build_result)

        all_responses = [sr.response for sr in step_results if sr.response]
        return TaskResult(
            task_name=task.name,
            prompt_sent=prompt_sent,
            response="\n".join(all_responses),
            success=build_result.success,
            step_results=step_results,
        )

    def _store_artifact(self, task: Task, task_result: TaskResult) -> None:
        """Store task output as an artifact for downstream access."""
        artifact_name = task.options.register or task.name

        # Collect stdout and stderr from shell steps
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        last_exit_code = "0"
        response_parts: list[str] = []

        for sr in task_result.step_results:
            if sr.kind == "shell":
                stdout_parts.append(sr.response)
            elif sr.kind == "prompt":
                response_parts.append(sr.response)
            if not sr.success:
                last_exit_code = "1"

        self.artifacts[artifact_name] = {
            "stdout": "\n".join(stdout_parts).strip(),
            "stderr": "",  # stderr is mixed into stdout in current implementation
            "exit_code": last_exit_code if not task_result.success else "0",
            "success": "true" if task_result.success else "false",
            "response": "\n".join(response_parts).strip(),
        }

    def _fire_webhook(self, task: Task, task_result: TaskResult, elapsed: float) -> None:
        """Send a webhook notification if configured."""
        webhook_url = task.options.webhook
        if not webhook_url:
            return

        status = "success" if task_result.success else "failure"
        webhook_on = task.options.webhook_on

        if webhook_on == "success" and not task_result.success:
            return
        if webhook_on == "failure" and task_result.success:
            return

        payload = {
            "task": task.name,
            "status": status,
            "exit_code": 0 if task_result.success else 1,
            "stdout": task_result.response[:4096],  # limit size
            "duration_ms": int(elapsed * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except (urllib.error.URLError, OSError) as e:
            if self.verbose:
                _log(f"         webhook failed: {e}", dim=True)
