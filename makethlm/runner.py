"""Task runner with dependency resolution, SSH execution, and LLM routing."""

from __future__ import annotations

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import HostGroup, LLMProvider, Promptfile, SecretError, Task, TaskStep, evaluate_condition, parse_duration_seconds
from .dispatcher import Dispatcher, DispatchResult, ClaudeDispatcher, CodexDispatcher, ShellDispatcher
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
    try:
        return parse_duration_seconds(duration)
    except ValueError:
        raise ValueError(f"invalid cache duration: {duration!r} (expected e.g. '1h', '30m', '1d')")


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
    exit_code: int | None = None


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
    if group.identity_file:
        identity_file = os.path.expandvars(os.path.expanduser(group.identity_file))
        parts.extend(["-i", shlex.quote(identity_file)])
    if group.port:
        parts.extend(["-p", str(group.port)])
    parts.append("-o")
    parts.append("BatchMode=yes")
    if group.strict_host_key_checking:
        parts.extend(["-o", f"StrictHostKeyChecking={group.strict_host_key_checking}"])
    target = f"{group.user}@{host}" if group.user else host
    parts.append(target)
    parts.append(command)
    return " ".join(parts)


def _dispatcher_for_provider(provider: LLMProvider) -> Dispatcher:
    """Create a Dispatcher from an LLMProvider configuration."""
    if provider.shell_template:
        return ShellDispatcher(provider.shell_template)
    if provider.name.lower() == "codex":
        return CodexDispatcher(model=provider.model)
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
        dry_run: bool = False,
    ):
        self.pf = pf
        self.dispatcher = dispatcher  # fallback/default dispatcher
        self._dotenv_loaded = False
        self._exports_applied = False
        self.quiet = quiet  # global quiet from CLI or settings
        self.verbose = verbose and not quiet  # show progress unless quiet
        self.promptfile_path = promptfile_path
        self.dry_run = dry_run
        self.artifacts: dict[str, dict[str, str]] = {}  # task_name -> {stdout, stderr, exit_code, success, response}
        self._cache_dir = Path(os.path.expanduser("~/.cache/makethlm"))
        self._rollback_stack: set[str] = set()
        self._runtime_secret_values: set[str] = set()

    def _cache_key(self, task: Task) -> str:
        """Generate a cache key for a task based on its name and inputs."""
        h = hashlib.sha256()
        h.update(task.name.encode())
        # Include step content in the hash
        for step in task.steps:
            h.update(step.content.encode())
        # Include resolved variables used by this task
        return h.hexdigest()[:16]

    def _register_secret_value(self, value: str) -> None:
        """Record a resolved secret for later redaction."""
        if value:
            self._runtime_secret_values.add(value)

    def _get_cached_result(self, task: Task) -> TaskResult | None:
        """Return a cached TaskResult if valid, else None."""
        if not task.options.cache or self._task_uses_secret_resolution(task):
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
        if self.dry_run:
            return self.dispatcher

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

    def _shell_timeout(self, task: Task | None) -> float:
        """Return the effective timeout for local shell and SSH steps."""
        if task and task.options.timeout:
            return parse_duration_seconds(task.options.timeout)
        return 120

    def _secret_values(self) -> list[str]:
        """Return likely secret values for log/artifact redaction."""
        secret_keys = re.compile(r"(SECRET|TOKEN|PASSWORD|PASS|API_KEY|KEY)", re.IGNORECASE)
        values: set[str] = set()
        for key, value in os.environ.items():
            if secret_keys.search(key) and len(value) >= 4:
                values.add(value)
        for key, value in self.pf.get_exported_env().items():
            if secret_keys.search(key) and len(value) >= 4:
                values.add(value)
        values.update(v for v in self._runtime_secret_values if len(v) >= 4)
        return sorted(values, key=len, reverse=True)

    def _redact(self, text: str) -> str:
        """Redact likely secrets from command, prompt, webhook, and artifact output."""
        redacted = text
        for value in self._secret_values():
            redacted = redacted.replace(value, "[redacted]")
        return redacted

    @staticmethod
    def _effective_host_group(task: Task, host_group: HostGroup) -> HostGroup:
        """Return host settings with task-level SSH overrides applied."""
        return HostGroup(
            name=host_group.name,
            hosts=list(host_group.hosts),
            user=host_group.user,
            port=host_group.port,
            identity_file=task.options.ssh_identity or host_group.identity_file,
            strict_host_key_checking=(
                task.options.ssh_strict_host_key_checking
                or host_group.strict_host_key_checking
            ),
            timeout=task.options.timeout,
            line_number=host_group.line_number,
        )

    def _should_echo(self, task: Task, step: TaskStep) -> bool:
        """Return True if a command should be echoed before execution."""
        if step.quiet:
            return False
        if task.options.no_quiet:
            return True
        if self.quiet or self.pf.settings.quiet:
            return False
        return True

    @staticmethod
    def _step_runtime_values(sr: StepResult) -> dict[str, str]:
        """Return interpolation values for a completed shell/SSH step."""
        return {
            "stdout": sr.response,
            "stderr": "",
            "output": sr.response,
            "exit_code": str(sr.exit_code if sr.exit_code is not None else (0 if sr.success else 1)),
            "success": "true" if sr.success else "false",
        }

    def _record_step_context(
        self,
        context: dict[str, str],
        step: TaskStep,
        sr: StepResult,
        step_index: int,
    ) -> None:
        """Expose a completed shell/SSH step to later prompt interpolation."""
        if sr.kind not in ("shell", "ssh", "docker-build"):
            return

        values = self._step_runtime_values(sr)
        for key, value in values.items():
            context[f"last.{key}"] = value
            context[f"step{step_index}.{key}"] = value
            if step.capture:
                context[f"{step.capture}.{key}"] = value

    @staticmethod
    def _format_pipe_context(step: TaskStep, sr: StepResult) -> str:
        label = step.capture or "previous command"
        return f"Shell output from {label}:\n{sr.response}".strip()

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

    def _run_rollback(self, failed_task: Task, result: RunResult) -> None:
        """Run a task's configured rollback hook after failure."""
        rollback_target = failed_task.options.rollback
        if not rollback_target:
            return
        rollback_target = self.pf.resolve_alias(rollback_target)
        if failed_task.name in self._rollback_stack or rollback_target in self._rollback_stack:
            if self.verbose:
                _log(f"         rollback skipped for {failed_task.name} (cycle guard)", dim=True)
            return
        if rollback_target not in self.pf.tasks:
            if self.verbose:
                _log(f"         rollback target not found: {rollback_target}", dim=True)
            return

        if self.verbose:
            _log(f"         running rollback task {rollback_target} for {failed_task.name}", dim=True)

        self._rollback_stack.update({failed_task.name, rollback_target})
        try:
            rollback_result = self.run(rollback_target)
            result.task_results.extend(rollback_result.task_results)
        finally:
            self._rollback_stack.discard(failed_task.name)
            self._rollback_stack.discard(rollback_target)

    def _task_uses_secret_resolution(self, task: Task) -> bool:
        """Return True if a task declares or references secrets."""
        if any("{{#secret:" in step.content for step in task.steps):
            return True
        agent = self.pf.get_agent_for_task(task.name)
        if agent and "{{#secret:" in agent.instructions:
            return True
        return bool(self.pf.guidance and "{{#secret:" in self.pf.guidance)

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
                self._run_rollback(task, result)
                break

        return result

    def run_parallel(
        self,
        target: str | None = None,
        args: dict[str, str] | None = None,
        *,
        jobs: int | None = None,
    ) -> RunResult:
        """Run a target task with independent tasks at each level in parallel.

        Tasks in the same dependency level are eligible to run concurrently.
        ``jobs`` limits concurrent task workers; ``None`` means no explicit cap.
        """
        if jobs is not None and jobs < 1:
            raise ValueError("jobs must be at least 1")

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
                level_results = self._run_level_parallel(level, target, args, idx, total, result, jobs)
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

        if not task_result.success:
            self._run_rollback(task, result)

        return task_result

    def _run_level_parallel(
        self,
        level: list[str],
        target: str,
        args: dict[str, str] | None,
        start_idx: int,
        total: int,
        result: RunResult,
        jobs: int | None = None,
    ) -> list[TaskResult | None]:
        """Run all tasks in a dependency level concurrently."""
        max_workers = len(level) if jobs is None else min(jobs, len(level))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    self._run_single_task_in_pipeline,
                    task_name,
                    target,
                    args,
                    start_idx + i + 1,
                    total,
                    result,
                )
                for i, task_name in enumerate(level)
            ]
            return [future.result() for future in futures]

    def _run_task(self, task: Task, args: dict[str, str] | None) -> TaskResult:
        """Execute a single task's steps."""
        resolved_steps = self.pf.resolve_steps(
            task.name,
            args,
            promptfile_path=self.promptfile_path,
            mask_secrets=self.dry_run,
            secret_callback=self._register_secret_value if not self.dry_run else None,
        )
        prompt_sent = self.pf.resolve_prompt(
            task.name,
            args,
            promptfile_path=self.promptfile_path,
            mask_secrets=self.dry_run,
            secret_callback=self._register_secret_value if not self.dry_run else None,
        )

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
        prompt_parts: list[str] = []
        runtime_context: dict[str, str] = {}
        pending_pipe_outputs: list[str] = []
        success = True
        dispatcher = self._get_dispatcher(task)
        working_dir = self._resolve_working_dir(task)

        for step_index, step in enumerate(resolved_steps, 1):
            if step.kind == "echo":
                _log(f"         {self._redact(step.content)}")
                sr = StepResult(kind="echo", content=self._redact(step.content), response="", success=True)
            elif step.kind == "shell":
                if self.verbose:
                    redacted_cmd = self._redact(step.content)
                    cmd_preview = redacted_cmd if len(redacted_cmd) <= 60 else redacted_cmd[:57] + "..."
                    _log(f"         $ {cmd_preview}", dim=True)
                if self.dry_run:
                    sr = StepResult(kind="shell", content=self._redact(step.content), response="", success=True)
                else:
                    sr = self._run_shell_step(step, task=task, working_dir=working_dir)
                self._record_step_context(runtime_context, step, sr, step_index)
                if step.pipe_output and sr.response:
                    pending_pipe_outputs.append(self._format_pipe_context(step, sr))
            else:
                if self.verbose:
                    prompt_preview = step.content.split("\n")[0]
                    if len(prompt_preview) > 60:
                        prompt_preview = prompt_preview[:57] + "..."
                    _log(f"         > sending prompt to LLM ...", dim=True)
                pipe_context = "\n\n".join(pending_pipe_outputs)
                pending_pipe_outputs.clear()
                sr = self._run_prompt_step(step, task, dispatcher, runtime_context, pipe_context)
                prompt_parts.append(sr.content)

            step_results.append(sr)
            all_responses.append(sr.response)

            if not sr.success:
                success = False
                if not task.options.no_exit_message:
                    pass  # normal error reporting
                break

        return TaskResult(
            task_name=task.name,
            prompt_sent=self._redact("\n\n".join(prompt_parts) or prompt_sent),
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
        prompt_parts: list[str] = []
        runtime_context: dict[str, str] = {}
        pending_pipe_outputs: list[str] = []
        success = True
        dispatcher = self._get_dispatcher(task)
        effective_group = self._effective_host_group(task, host_group)

        for step_index, step in enumerate(resolved_steps, 1):
            if step.kind == "echo":
                _log(f"         {self._redact(step.content)}")
                sr = StepResult(kind="echo", content=self._redact(step.content), response="", success=True)
                step_results.append(sr)
                continue
            elif step.kind == "shell":
                # Execute on each host. In parallel mode all hosts for this
                # step are attempted; failure stops the next task step.
                if self.dry_run:
                    for host in effective_group.hosts:
                        step_results.append(StepResult(
                            kind="ssh",
                            content=self._redact(step.content),
                            response="",
                            success=True,
                            host=host,
                        ))
                elif task.options.ssh_parallel and len(effective_group.hosts) > 1:
                    if self.verbose:
                        _log(f"         $ {self._redact(step.content)} (on {len(effective_group.hosts)} hosts in parallel)", dim=True)
                    with ThreadPoolExecutor(max_workers=len(effective_group.hosts)) as executor:
                        host_results = list(
                            executor.map(
                                lambda h: self._run_ssh_step(step, h, effective_group),
                                effective_group.hosts,
                            )
                        )
                    step_results.extend(host_results)
                    all_responses.extend(sr.response for sr in host_results)
                    combined = StepResult(
                        kind="ssh",
                        content=self._redact(step.content),
                        response="\n".join(sr.response for sr in host_results if sr.response).strip(),
                        success=not any(not sr.success for sr in host_results),
                        exit_code=next((sr.exit_code for sr in host_results if sr.exit_code), 0),
                    )
                    self._record_step_context(runtime_context, step, combined, step_index)
                    if step.pipe_output and combined.response:
                        pending_pipe_outputs.append(self._format_pipe_context(step, combined))
                    if any(not sr.success for sr in host_results):
                        success = False
                else:
                    host_step_results: list[StepResult] = []
                    for host in effective_group.hosts:
                        if self.verbose:
                            _log(f"         $ {self._redact(step.content)} (on {host})", dim=True)
                        sr = self._run_ssh_step(step, host, effective_group)
                        step_results.append(sr)
                        all_responses.append(sr.response)
                        host_step_results.append(sr)
                        if not sr.success:
                            success = False
                            break
                    combined = StepResult(
                        kind="ssh",
                        content=self._redact(step.content),
                        response="\n".join(sr.response for sr in host_step_results if sr.response).strip(),
                        success=not any(not sr.success for sr in host_step_results),
                        exit_code=next((sr.exit_code for sr in host_step_results if sr.exit_code), 0),
                    )
                    self._record_step_context(runtime_context, step, combined, step_index)
                    if step.pipe_output and combined.response:
                        pending_pipe_outputs.append(self._format_pipe_context(step, combined))
                if not success:
                    break
            else:
                # Prompt steps still run locally via LLM
                if self.verbose:
                    _log(f"         > sending prompt to LLM ...", dim=True)
                pipe_context = "\n\n".join(pending_pipe_outputs)
                pending_pipe_outputs.clear()
                sr = self._run_prompt_step(step, task, dispatcher, runtime_context, pipe_context)
                step_results.append(sr)
                all_responses.append(sr.response)
                prompt_parts.append(sr.content)
                if not sr.success:
                    success = False
                    break

        return TaskResult(
            task_name=task.name,
            prompt_sent=self._redact("\n\n".join(prompt_parts) or prompt_sent),
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
            timeout = self._shell_timeout(task)
            proc = _run_subprocess(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
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
                content=self._redact(step.content),
                response=self._redact(output.strip()) if not step.silent else "",
                success=ok,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            timeout = self._shell_timeout(task)
            return StepResult(
                kind="shell",
                content=self._redact(step.content),
                response=f"error: command timed out after {_fmt_elapsed(timeout)}",
                success=step.ignore_error,
                exit_code=124,
            )

    def _run_ssh_step(self, step: TaskStep, host: str, group: HostGroup) -> StepResult:
        """Execute a shell command on a remote host via SSH."""
        ssh_cmd = _build_ssh_command(host, step.content, group)
        timeout = parse_duration_seconds(group.timeout) if group.timeout else 120
        try:
            proc = _run_subprocess(
                ssh_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = proc.stdout
            if proc.stderr:
                output += proc.stderr
            ok = proc.returncode == 0 or step.ignore_error
            return StepResult(
                kind="ssh",
                content=self._redact(step.content),
                response=self._redact(output.strip()) if not step.silent else "",
                success=ok,
                host=host,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return StepResult(
                kind="ssh",
                content=self._redact(step.content),
                response=f"error: SSH to {host} timed out after {_fmt_elapsed(timeout)}",
                success=step.ignore_error,
                host=host,
                exit_code=124,
            )

    def _run_prompt_step(
        self,
        step: TaskStep,
        task: Task,
        dispatcher: Dispatcher,
        runtime_context: dict[str, str] | None = None,
        pipe_context: str | None = None,
    ) -> StepResult:
        """Send a prompt step to the LLM dispatcher."""
        prompt = step.content
        if runtime_context:
            prompt = self.pf._interpolate(prompt, runtime_context)
        if pipe_context:
            prompt = f"{pipe_context}\n\n{prompt}"
        dr = dispatcher.dispatch(prompt, task)
        return StepResult(
            kind="prompt",
            content=self._redact(prompt),
            response=self._redact(dr.response),
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
        if self.dry_run:
            step_results.append(StepResult(
                kind="docker-generate",
                content=self._redact(generate_prompt),
                response="[dry-run] generate Dockerfile",
                success=True,
            ))
            build_cmd = f"docker build -t {task.name}:{docker.tag} -f {os.path.join(docker.context, docker.file)} {docker.context}"
            step_results.append(StepResult(
                kind="docker-build",
                content=build_cmd,
                response="[dry-run] build Docker image",
                success=True,
            ))
            return TaskResult(
                task_name=task.name,
                prompt_sent=self._redact(prompt_sent),
                response="\n".join(sr.response for sr in step_results),
                success=True,
                step_results=step_results,
            )

        dr = dispatcher.dispatch(generate_prompt, task)
        step_results.append(StepResult(
            kind="docker-generate",
            content=self._redact(generate_prompt),
            response=self._redact(dr.response),
            success=dr.success,
        ))

        if not dr.success:
            return TaskResult(
                task_name=task.name,
                prompt_sent=self._redact(prompt_sent),
                response=self._redact(dr.response),
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
                prompt_sent=self._redact(prompt_sent),
                response=f"error writing Dockerfile: {e}",
                success=False,
                step_results=step_results,
            )

        tag = f"{task.name}:{docker.tag}"
        build_cmd = f"docker build -t {tag} -f {dockerfile_path} {docker.context}"
        build_step = TaskStep(kind="shell", content=build_cmd)
        build_result = self._run_shell_step(build_step, task=task)
        build_result.kind = "docker-build"
        step_results.append(build_result)

        all_responses = [sr.response for sr in step_results if sr.response]
        return TaskResult(
            task_name=task.name,
            prompt_sent=self._redact(prompt_sent),
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
            if sr.exit_code is not None:
                last_exit_code = str(sr.exit_code)
            elif not sr.success:
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
            "stdout": self._redact(task_result.response)[:4096],  # limit size
            "duration_ms": int(elapsed * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            preset = None
            url = webhook_url
            if ":" in webhook_url:
                maybe_preset, maybe_url = webhook_url.split(":", 1)
                if maybe_preset in ("ntfy", "gotify", "discord", "slack") and maybe_url.startswith(("http://", "https://")):
                    preset = maybe_preset
                    url = maybe_url

            if preset == "ntfy":
                body = f"{task.name}: {status}\n{payload['stdout']}".encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers={
                        "Title": f"makethlm {task.name}",
                        "Tags": "white_check_mark" if task_result.success else "warning",
                    },
                    method="POST",
                )
            elif preset == "gotify":
                data = json.dumps({
                    "title": f"makethlm {task.name}: {status}",
                    "message": payload["stdout"],
                    "priority": 5 if task_result.success else 8,
                }).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            elif preset in ("discord", "slack"):
                data = json.dumps({
                    "content" if preset == "discord" else "text": (
                        f"makethlm `{task.name}` {status}\n{payload['stdout']}"
                    )
                }).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            else:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            urllib.request.urlopen(req, timeout=10)
        except (urllib.error.URLError, OSError) as e:
            if self.verbose:
                _log(f"         webhook failed: {e}", dim=True)
