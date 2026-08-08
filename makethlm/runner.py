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
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .calllog import CallLog, CallRecord
from .contracts import required_artifact_error, split_artifact_contract, value_matches
from .cost import CostTotals, derive_cost, parse_cost
from .dispatcher import (
    ClaudeDispatcher,
    CodexDispatcher,
    Dispatcher,
    DispatchResult,
    OllamaDispatcher,
    OpenAIDispatcher,
    OpenCodeDispatcher,
    ShellDispatcher,
)
from .docker import (
    docker_build_argv,
    docker_dry_run_build_command,
    docker_generate_prompt,
    format_docker_build_command,
    resolve_dockerfile_path,
    run_docker_build,
    strip_dockerfile_markdown_fence,
)
from .fixtures import FixtureStore
from .models import (
    MAX_FALLBACK_LLMS,
    MAX_LLM_RETRIES,
    MAX_REPAIR_ATTEMPTS,
    HostGroup,
    LLMProvider,
    Promptfile,
    Task,
    TaskStep,
    evaluate_condition,
    parse_duration_seconds,
)
from .progress import ElapsedIndicator
from .ratelimit import is_rate_limited, rate_limit_backoff
from .sandbox import build_sandbox_command
from .secrets import is_secret_name, redact_text, secret_values_from_mapping
from .ssh import build_ssh_argv, build_ssh_command, run_ssh_command
from .staleness import digest_sources, up_to_date_reason
from .subprocess_util import run_subprocess as _run_subprocess
from .webhooks import send_webhook


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
        for dep in pf.tasks[name].subsequent_dependencies:
            visit(dep, path)

    visit(target, [])
    return order


def topological_levels(pf: Promptfile, target: str) -> list[list[str]]:
    """Return tasks grouped into levels for parallel execution.

    Tasks at the same level have no dependencies on each other and can
    be executed concurrently.
    """
    order = topological_sort(pf, target)
    if any(pf.tasks[name].subsequent_dependencies for name in order):
        return [[name] for name in order]

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


MAX_REPAIR_ECHO_CHARS = 2000

_CONTRACT_REPAIR_HINTS = {
    "json": "a single valid JSON value",
    "object": "a single valid JSON object",
    "array": "a single valid JSON array",
    "integer": "a single integer with no other characters",
    "number": "a single number with no other characters",
    "boolean": "exactly true or false",
    "nonempty": "a non-empty answer",
    "text": "a text answer",
}


def format_fanout_response(results: list[tuple[str, DispatchResult]]) -> str:
    """Return every fan-out answer, labeled by provider."""
    sections = []
    for name, outcome in results:
        status = "" if outcome.success else " (failed)"
        sections.append(f"[{name}{status}]\n{outcome.response.strip()}")
    return "\n\n".join(sections)


def build_judge_prompt(prompt: str, answers: list[tuple[str, str]]) -> str:
    """Return the prompt asking a judge provider to merge fan-out answers."""
    sections = [f"--- answer from {name} ---\n{text.strip()}" for name, text in answers]
    joined = "\n\n".join(sections)
    return (
        f"{len(answers)} models were given the same task. Merge their answers into a "
        f"single best response.\n\n"
        f"Original task:\n{prompt}\n\n"
        f"{joined}\n\n"
        "Reply with the merged answer only. Prefer claims the models agree on, drop "
        "anything contradicted or unsupported, and do not mention the models or that "
        "a merge took place."
    )


def _last_prompt_index(steps: list[TaskStep]) -> int | None:
    """Return the 1-based index of the final prompt step, if any.

    Only that step is validated against ``produces`` during execution: it is
    the one whose response the contract can still be repaired through.
    """
    indexes = [i for i, step in enumerate(steps, 1) if step.kind not in ("echo", "shell")]
    return indexes[-1] if indexes else None


def build_repair_prompt(prompt: str, expected: str, previous: str) -> str:
    """Return a re-prompt asking the provider to satisfy an output contract."""
    wanted = _CONTRACT_REPAIR_HINTS.get(expected, f"a value of type {expected}")
    echoed = previous.strip()
    if len(echoed) > MAX_REPAIR_ECHO_CHARS:
        echoed = echoed[:MAX_REPAIR_ECHO_CHARS] + "\n[...truncated]"
    return (
        f"{prompt}\n\n"
        f"Your previous response did not satisfy the required output contract "
        f"produces={expected}. It must be {wanted}, with no prose, explanation, "
        f"or code fences around it.\n\n"
        f"Previous response:\n{echoed}\n\n"
        f"Reply with the corrected output only."
    )


def _parse_task_cost(value: str | None) -> float | None:
    """Return a task's budget in USD, ignoring values the parser rejected."""
    if not value:
        return None
    try:
        return parse_cost(value)
    except ValueError:
        return None


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
    provider: str | None = None
    attempt: int | None = None
    variants: dict[str, str] = field(default_factory=dict)  # provider -> answer, for fan-out


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


def _build_ssh_argv(host: str, command: str, group: HostGroup) -> list[str]:
    """Build SSH argv for remote execution."""
    return build_ssh_argv(host, command, group)


def _build_ssh_command(host: str, command: str, group: HostGroup) -> str:
    """Build a shell-escaped SSH command string for display/backward compatibility."""
    return build_ssh_command(host, command, group)


def _resolve_dockerfile_path(context: str, dockerfile: str) -> tuple[Path, Path]:
    """Return resolved context and Dockerfile paths, rejecting path escapes."""
    return resolve_dockerfile_path(context, dockerfile)


def _dispatcher_for_provider(provider: LLMProvider) -> Dispatcher:
    """Create a Dispatcher from an LLMProvider configuration."""
    if provider.shell_template:
        return ShellDispatcher(provider.shell_template)
    provider_name = provider.name.lower()
    if provider_name == "codex":
        return CodexDispatcher(model=provider.model)
    if provider_name == "openai":
        return OpenAIDispatcher(
            model=provider.model,
            api_key=provider.api_key,
            base_url=provider.base_url,
        )
    if provider_name == "ollama":
        return OllamaDispatcher(model=provider.model, base_url=provider.base_url)
    if provider_name == "opencode":
        return OpenCodeDispatcher(model=provider.model)
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
        always_make: bool = False,
        fixtures_dir: str | None = None,
        record_fixtures: bool = False,
        max_cost: float | None = None,
        call_log_path: str | None = None,
    ):
        self.pf = pf
        self.dispatcher = dispatcher  # fallback/default dispatcher
        self._dotenv_loaded = False
        self._exports_applied = False
        self.quiet = quiet  # global quiet from CLI or settings
        self.verbose = verbose and not quiet  # show progress unless quiet
        self.promptfile_path = promptfile_path
        self.dry_run = dry_run
        self.always_make = always_make  # ignore staleness and cache skips
        self.fixtures = FixtureStore(fixtures_dir) if fixtures_dir else None
        self.record_fixtures = record_fixtures and self.fixtures is not None
        self.max_cost = max_cost  # run-wide spend limit in USD
        self.call_log = CallLog(call_log_path) if call_log_path else None
        self.costs = CostTotals()
        self._cost_lock = threading.Lock()
        self._call_index = 0
        self._provider_limiters: dict[str, threading.Semaphore] = {}
        self._limiter_lock = threading.Lock()
        self.artifacts: dict[
            str, dict[str, str]
        ] = {}  # task_name -> {stdout, stderr, exit_code, success, response}
        self._cache_dir = Path(os.path.expanduser("~/.cache/makethlm"))
        self._rollback_stack: set[str] = set()
        self._postmortem_stack: set[str] = set()
        self._runtime_secret_values: set[str] = set()
        for task in self.pf.tasks.values():
            self._register_named_secret_values(task.options.env)
            self._register_named_secret_values(task.local_variables)
        for provider in self.pf.llm_providers.values():
            if provider.api_key:
                self._register_secret_value(provider.api_key)

    def _cache_payload(
        self,
        task: Task,
        args: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Return the execution inputs that determine a cached task result."""
        agent = self.pf.get_agent_for_task(task.name)
        expanded_sources = self.pf.task_secret_sources(task.name, args)
        raw_text = "\n".join(
            [
                *(step.content for step in task.steps),
                task.guidance or "",
                self.pf.guidance or "",
                agent.instructions if agent else "",
                *task.options.env.values(),
                *expanded_sources,
            ]
        )
        env_names = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", raw_text))
        env_names.update(re.findall(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)", raw_text))
        env_names.update(
            re.findall(
                r'env_var\(\s*["\']([A-Za-z_][A-Za-z0-9_]*)["\']',
                raw_text,
            )
        )
        provider = self.pf.get_llm_for_task(task.name)
        if agent and agent.llm:
            provider = self.pf.llm_providers.get(agent.llm, provider)
        artifact_name = task.options.register or task.name
        own_artifact_prefix = f"{artifact_name}."
        fallback = {
            "type": type(self.dispatcher).__name__,
            "model": getattr(self.dispatcher, "default_model", None),
            "template": getattr(self.dispatcher, "template", None),
        }
        return {
            "schema": 2,
            "task": task.name,
            "steps": [asdict(step) for step in task.steps],
            "expanded_sources": expanded_sources,
            "arguments": args or {},
            "variables": {
                name: value
                for name, value in self.pf.variables.items()
                if not name.startswith(own_artifact_prefix)
            },
            "task_variables": task.local_variables,
            "artifacts": {
                name: value for name, value in self.artifacts.items() if name != artifact_name
            },
            "options": asdict(task.options),
            "docker": asdict(task.docker) if task.docker else None,
            "provider": asdict(provider) if provider else fallback,
            "fallback_providers": {
                name: asdict(self.pf.llm_providers[name])
                for name in task.options.fallback_llms
                if name in self.pf.llm_providers
            },
            "agent": asdict(agent) if agent else None,
            "guidance": self.pf.guidance,
            "task_guidance": task.guidance,
            "environment": {name: os.environ.get(name) for name in sorted(env_names)},
            "sources": digest_sources(task.options.sources, self._resolve_working_dir(task)),
        }

    def _cache_key(self, task: Task, args: dict[str, str] | None = None) -> str:
        """Generate a stable cache key from all known execution inputs."""
        payload = json.dumps(
            self._cache_payload(task, args),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def _cache_file(
        self,
        task: Task,
        args: dict[str, str] | None = None,
    ) -> Path:
        """Return the cache file path without trusting task names as path data."""
        return self._cache_dir / f"{self._cache_key(task, args)}.json"

    def _register_secret_value(self, value: str) -> None:
        """Record a resolved secret for later redaction."""
        if value:
            self._runtime_secret_values.add(value)

    def _register_named_secret_values(self, values: dict[str, str]) -> None:
        """Record secret-like values from a named mapping."""
        self._runtime_secret_values.update(
            value for name, value in values.items() if is_secret_name(name) and value
        )

    def _up_to_date_result(self, task: Task) -> TaskResult | None:
        """Return a skip result when the task's outputs are newer than its sources."""
        if self.always_make or self.dry_run:
            return None
        reason = up_to_date_reason(
            task.options.sources,
            task.options.outputs,
            self._resolve_working_dir(task),
        )
        if reason is None:
            return None
        return TaskResult(
            task_name=task.name,
            prompt_sent="",
            response=f"[skipped] {reason}",
            success=True,
        )

    def _get_cached_result(
        self,
        task: Task,
        args: dict[str, str] | None = None,
    ) -> TaskResult | None:
        """Return a cached TaskResult if valid, else None."""
        if self.always_make:
            return None
        if not task.options.cache or self._task_uses_secret_resolution(task, args):
            return None
        try:
            duration = _parse_cache_duration(task.options.cache)
        except ValueError:
            return None

        cache_file = self._cache_file(task, args)
        if not cache_file.exists():
            return None

        try:
            data = json.loads(cache_file.read_text())
            if data.get("schema") != 2:
                return None
            cached_at = data.get("cached_at", 0)
            if time.time() - cached_at > duration:
                cache_file.unlink(missing_ok=True)
                return None
            return TaskResult(
                task_name=data["task_name"],
                prompt_sent=data.get("prompt_sent", ""),
                response=data.get("response", ""),
                success=True,
                step_results=[
                    StepResult(
                        kind=step["kind"],
                        content=step.get("content", ""),
                        response=step.get("response", ""),
                        success=step.get("success", True),
                        host=step.get("host"),
                        exit_code=step.get("exit_code"),
                        provider=step.get("provider"),
                        attempt=step.get("attempt"),
                    )
                    for step in data.get("step_results", [])
                ],
            )
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            return None

    def _save_cached_result(
        self,
        task: Task,
        result: TaskResult,
        args: dict[str, str] | None = None,
    ) -> None:
        """Save a task result to the cache."""
        if (
            not task.options.cache
            or not result.success
            or self._task_uses_secret_resolution(task, args)
        ):
            return
        cache_tmp_path: Path | None = None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._cache_file(task, args)
            data = {
                "schema": 2,
                "task_name": result.task_name,
                "prompt_sent": result.prompt_sent,
                "response": result.response,
                "step_results": [asdict(step) for step in result.step_results],
                "cached_at": time.time(),
            }
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self._cache_dir,
                prefix=".cache-",
                suffix=".tmp",
                delete=False,
            ) as cache_tmp:
                cache_tmp.write(json.dumps(data))
                cache_tmp_path = Path(cache_tmp.name)
            os.chmod(cache_tmp_path, 0o600)
            os.replace(cache_tmp_path, cache_file)
        except OSError:
            pass
        finally:
            if cache_tmp_path is not None:
                cache_tmp_path.unlink(missing_ok=True)

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
        values = secret_values_from_mapping(dict(os.environ))
        values.update(
            value
            for name, value in {
                **self.pf.variables,
                **self.pf.get_exported_env(),
            }.items()
            if is_secret_name(name) and value
        )
        values.update(self._runtime_secret_values)
        return sorted(values, key=len, reverse=True)

    def _redact(self, text: str) -> str:
        """Redact likely secrets from command, prompt, webhook, and artifact output."""
        return redact_text(text, self._secret_values())

    @staticmethod
    def _effective_host_group(
        task: Task,
        host_group: HostGroup,
        host: str | None = None,
    ) -> HostGroup:
        """Return host settings with task-level SSH overrides applied."""
        connection = host_group.connections.get(host) if host else None
        return HostGroup(
            name=host_group.name,
            hosts=[host] if host else list(host_group.hosts),
            user=connection.user if connection and connection.user else host_group.user,
            port=connection.port if connection and connection.port else host_group.port,
            identity_file=(
                task.options.ssh_identity
                or (connection.identity_file if connection else None)
                or host_group.identity_file
            ),
            strict_host_key_checking=(
                task.options.ssh_strict_host_key_checking
                or (connection.strict_host_key_checking if connection else None)
                or host_group.strict_host_key_checking
            ),
            timeout=task.options.timeout,
            line_number=host_group.line_number,
            connections=dict(host_group.connections),
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
            "exit_code": str(
                sr.exit_code if sr.exit_code is not None else (0 if sr.success else 1)
            ),
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
        if step.kind == "prompt":
            source = sr.provider or "the previous model"
            return f"Answer from {source}:\n{sr.response}".strip()
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
            _log(
                f"         running rollback task {rollback_target} for {failed_task.name}", dim=True
            )

        self._rollback_stack.update({failed_task.name, rollback_target})
        try:
            rollback_result = self.run(rollback_target)
            result.task_results.extend(rollback_result.task_results)
        finally:
            self._rollback_stack.discard(failed_task.name)
            self._rollback_stack.discard(rollback_target)

    def _run_postmortem(self, failed_task: Task, result: RunResult) -> None:
        """Run a diagnostic task after failure, preserving its result in the run."""
        target = failed_task.options.postmortem
        if not target:
            return
        target = self.pf.resolve_alias(target)
        if failed_task.name in self._postmortem_stack or target in self._postmortem_stack:
            if self.verbose:
                _log(
                    f"         postmortem skipped for {failed_task.name} (cycle guard)",
                    dim=True,
                )
            return
        if target not in self.pf.tasks:
            if self.verbose:
                _log(f"         postmortem target not found: {target}", dim=True)
            return
        if self.verbose:
            _log(
                f"         running postmortem task {target} for {failed_task.name}",
                dim=True,
            )
        self._postmortem_stack.update({failed_task.name, target})
        try:
            postmortem_result = self.run(target)
            result.task_results.extend(postmortem_result.task_results)
        finally:
            self._postmortem_stack.discard(failed_task.name)
            self._postmortem_stack.discard(target)

    def _run_failure_hooks(self, failed_task: Task, result: RunResult) -> None:
        """Run diagnostics before attempting rollback."""
        self._run_postmortem(failed_task, result)
        self._run_rollback(failed_task, result)

    def _task_uses_secret_resolution(
        self,
        task: Task,
        args: dict[str, str] | None = None,
    ) -> bool:
        """Return True if a task declares or references secrets."""
        return self.pf.task_references_secret(task.name, args)

    def run(self, target: str | None = None, args: dict[str, str] | None = None) -> RunResult:
        """Run a target task and all its dependencies."""
        self._ensure_dotenv()
        self._apply_exports()
        self._register_named_secret_values(args or {})

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
            task_args = args if task_name == target else None

            # OS filter: skip tasks not meant for this OS
            if task.options.should_skip_for_os():
                if self.verbose:
                    _log(
                        f"  [{idx}/{total}] {task_name} ... skipped (requires {task.options.os_filter})",
                        dim=True,
                    )
                result.task_results.append(
                    TaskResult(
                        task_name=task_name,
                        prompt_sent="",
                        response=f"[skipped] not applicable on this OS (requires {task.options.os_filter})",
                        success=True,
                    )
                )
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
                        _log(
                            f"  [{idx}/{total}] {task_name} ... skipped (when condition not met)",
                            dim=True,
                        )
                    result.task_results.append(
                        TaskResult(
                            task_name=task_name,
                            prompt_sent="",
                            response="[skipped] when condition not met",
                            success=True,
                        )
                    )
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
                    result.task_results.append(
                        TaskResult(
                            task_name=task_name,
                            prompt_sent="",
                            response="[skipped] user declined confirmation",
                            success=True,
                        )
                    )
                    return result

            # File staleness: skip when outputs are newer than sources
            fresh = self._up_to_date_result(task)
            if fresh is not None:
                if self.verbose:
                    reason = fresh.response.removeprefix("[skipped] ")
                    _log(f"  [{idx}/{total}] {task_name} ... {reason}", dim=True)
                result.task_results.append(fresh)
                self._store_skipped_artifact(task)
                continue

            # Check cache
            cached = self._get_cached_result(task, task_args)
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

            t0 = time.monotonic()
            task_result = self._run_task(task, task_args)
            elapsed = time.monotonic() - t0
            result.task_results.append(task_result)

            # Save to cache if configured
            self._save_cached_result(task, task_result, task_args)

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
                self._run_failure_hooks(task, result)
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
        self._register_named_secret_values(args or {})

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
            _log(f"Running {target} ({total} tasks, {len(levels)} levels)", bold=True)

        idx = 0
        for level in levels:
            if len(level) == 1:
                # Single task — run synchronously (no asyncio overhead)
                idx += 1
                task_name = level[0]
                task_result = self._run_single_task_in_pipeline(
                    task_name,
                    target,
                    args,
                    idx,
                    total,
                    result,
                )
                if task_result is not None and not task_result.success:
                    break
            else:
                # Multiple tasks — run in parallel
                level_results = self._run_level_parallel(
                    level, target, args, idx, total, result, jobs
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
        task_args = args if task_name == target else None

        # OS filter
        if task.options.should_skip_for_os():
            if self.verbose:
                _log(
                    f"  [{idx}/{total}] {task_name} ... skipped (requires {task.options.os_filter})",
                    dim=True,
                )
            tr = TaskResult(
                task_name=task_name,
                prompt_sent="",
                response="[skipped] not applicable on this OS",
                success=True,
            )
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
                    _log(
                        f"  [{idx}/{total}] {task_name} ... skipped (when condition not met)",
                        dim=True,
                    )
                tr = TaskResult(
                    task_name=task_name,
                    prompt_sent="",
                    response="[skipped] when condition not met",
                    success=True,
                )
                result.task_results.append(tr)
                self.artifacts[task_name] = {
                    "stdout": "",
                    "stderr": "",
                    "exit_code": "0",
                    "success": "skipped",
                    "response": "",
                }
                return tr

        # File staleness check
        fresh = self._up_to_date_result(task)
        if fresh is not None:
            if self.verbose:
                reason = fresh.response.removeprefix("[skipped] ")
                _log(f"  [{idx}/{total}] {task_name} ... {reason}", dim=True)
            result.task_results.append(fresh)
            self._store_skipped_artifact(task)
            return fresh

        # Cache check
        cached = self._get_cached_result(task, task_args)
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
                tr = TaskResult(
                    task_name=task_name,
                    prompt_sent="",
                    response="[skipped] user declined confirmation",
                    success=True,
                )
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

        t0 = time.monotonic()
        task_result = self._run_task(task, task_args)
        elapsed = time.monotonic() - t0
        result.task_results.append(task_result)

        self._save_cached_result(task, task_result, task_args)
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
            self._run_failure_hooks(task, result)

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
        self._register_named_secret_values(self._resolved_task_args(task, args))
        contract_error = self._required_artifact_error(task)
        if contract_error:
            return TaskResult(
                task_name=task.name,
                prompt_sent="",
                response=contract_error,
                success=False,
                step_results=[
                    StepResult(
                        kind="contract",
                        content="requires",
                        response=contract_error,
                        success=False,
                        exit_code=2,
                    )
                ],
            )
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
            result = self._run_docker_task(task, resolved_steps, prompt_sent)
            return self._apply_output_contract(task, result)

        host_group = self.pf.get_hosts_for_task(task.name)
        if host_group:
            result = self._run_on_hosts(
                task,
                resolved_steps,
                prompt_sent,
                host_group,
            )
            return self._apply_output_contract(task, result)

        result = self._run_local(task, resolved_steps, prompt_sent, args)
        return self._apply_output_contract(task, result)

    @staticmethod
    def _value_matches_contract(value: str, expected: str) -> bool:
        """Return whether a string value satisfies a supported contract type."""
        return value_matches(value, expected)

    @staticmethod
    def _split_artifact_contract(contract: str) -> tuple[str, str, str]:
        """Split ``artifact.field[:type]`` into its components."""
        return split_artifact_contract(contract)

    def _required_artifact_error(self, task: Task) -> str | None:
        """Return an actionable error for the first unmet input contract."""
        return required_artifact_error(task.options.requires, self.artifacts)

    def _apply_output_contract(
        self,
        task: Task,
        result: TaskResult,
    ) -> TaskResult:
        """Fail a successful task whose aggregate output has the wrong type."""
        expected = task.options.produces
        if (
            not expected
            or not result.success
            or self._value_matches_contract(result.response, expected)
        ):
            return result
        message = f"artifact contract failed: task {task.name!r} output is not {expected}"
        result.success = False
        result.response = "\n".join(value for value in (result.response, message) if value)
        result.step_results.append(
            StepResult(
                kind="contract",
                content=f"produces={expected}",
                response=message,
                success=False,
                exit_code=2,
            )
        )
        return result

    def _run_local(
        self,
        task: Task,
        resolved_steps: list[TaskStep],
        prompt_sent: str,
        args: dict[str, str] | None,
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
        resolved_task_args = self._resolved_task_args(task, args)
        positional_args = self._task_positional_args(task, resolved_task_args)

        last_prompt_index = _last_prompt_index(resolved_steps)

        for step_index, step in enumerate(resolved_steps, 1):
            if step.kind == "echo":
                _log(f"         {self._redact(step.content)}")
                sr = StepResult(
                    kind="echo", content=self._redact(step.content), response="", success=True
                )
            elif step.kind == "shell":
                if self.verbose:
                    redacted_cmd = self._redact(step.content)
                    cmd_preview = (
                        redacted_cmd if len(redacted_cmd) <= 60 else redacted_cmd[:57] + "..."
                    )
                    _log(f"         $ {cmd_preview}", dim=True)
                if self.dry_run:
                    sr = StepResult(
                        kind="shell", content=self._redact(step.content), response="", success=True
                    )
                else:
                    sr = self._run_shell_step(
                        step,
                        task=task,
                        working_dir=working_dir,
                        task_args=resolved_task_args,
                        positional_args=positional_args,
                    )
                self._record_step_context(runtime_context, step, sr, step_index)
                if step.pipe_output and sr.response:
                    pending_pipe_outputs.append(self._format_pipe_context(step, sr))
            else:
                if self.verbose:
                    prompt_preview = step.content.split("\n")[0]
                    if len(prompt_preview) > 60:
                        prompt_preview = prompt_preview[:57] + "..."
                    _log("         > sending prompt to LLM ...", dim=True)
                pipe_context = "\n\n".join(pending_pipe_outputs)
                pending_pipe_outputs.clear()
                sr = self._run_prompt_step(
                    step,
                    task,
                    dispatcher,
                    runtime_context,
                    pipe_context,
                    output_contract=(
                        task.options.produces if step_index == last_prompt_index else None
                    ),
                )
                prompt_parts.append(sr.content)
                if step.pipe_output and sr.response:
                    pending_pipe_outputs.append(self._format_pipe_context(step, sr))

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

        last_prompt_index = _last_prompt_index(resolved_steps)

        for step_index, step in enumerate(resolved_steps, 1):
            if step.kind == "echo":
                _log(f"         {self._redact(step.content)}")
                sr = StepResult(
                    kind="echo", content=self._redact(step.content), response="", success=True
                )
                step_results.append(sr)
                continue
            elif step.kind == "shell":
                # Execute on each host. In parallel mode all hosts for this
                # step are attempted; failure stops the next task step.
                if self.dry_run:
                    for host in effective_group.hosts:
                        step_results.append(
                            StepResult(
                                kind="ssh",
                                content=self._redact(step.content),
                                response="",
                                success=True,
                                host=host,
                            )
                        )
                elif task.options.ssh_parallel and len(effective_group.hosts) > 1:
                    if self.verbose:
                        _log(
                            f"         $ {self._redact(step.content)} (on {len(effective_group.hosts)} hosts in parallel)",
                            dim=True,
                        )
                    with ThreadPoolExecutor(max_workers=len(effective_group.hosts)) as executor:
                        host_results = list(
                            executor.map(
                                lambda h, current_step=step: self._run_ssh_step(
                                    current_step,
                                    h,
                                    self._effective_host_group(
                                        task,
                                        host_group,
                                        h,
                                    ),
                                ),
                                effective_group.hosts,
                            )
                        )
                    step_results.extend(host_results)
                    all_responses.extend(sr.response for sr in host_results)
                    combined = StepResult(
                        kind="ssh",
                        content=self._redact(step.content),
                        response="\n".join(
                            sr.response for sr in host_results if sr.response
                        ).strip(),
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
                        sr = self._run_ssh_step(
                            step,
                            host,
                            self._effective_host_group(
                                task,
                                host_group,
                                host,
                            ),
                        )
                        step_results.append(sr)
                        all_responses.append(sr.response)
                        host_step_results.append(sr)
                        if not sr.success:
                            success = False
                            break
                    combined = StepResult(
                        kind="ssh",
                        content=self._redact(step.content),
                        response="\n".join(
                            sr.response for sr in host_step_results if sr.response
                        ).strip(),
                        success=not any(not sr.success for sr in host_step_results),
                        exit_code=next(
                            (sr.exit_code for sr in host_step_results if sr.exit_code), 0
                        ),
                    )
                    self._record_step_context(runtime_context, step, combined, step_index)
                    if step.pipe_output and combined.response:
                        pending_pipe_outputs.append(self._format_pipe_context(step, combined))
                if not success:
                    break
            else:
                # Prompt steps still run locally via LLM
                if self.verbose:
                    _log("         > sending prompt to LLM ...", dim=True)
                pipe_context = "\n\n".join(pending_pipe_outputs)
                pending_pipe_outputs.clear()
                sr = self._run_prompt_step(
                    step,
                    task,
                    dispatcher,
                    runtime_context,
                    pipe_context,
                    output_contract=(
                        task.options.produces if step_index == last_prompt_index else None
                    ),
                )
                step_results.append(sr)
                all_responses.append(sr.response)
                prompt_parts.append(sr.content)
                if step.pipe_output and sr.response:
                    pending_pipe_outputs.append(self._format_pipe_context(step, sr))
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

    def _sandbox_command(
        self,
        cmd: str,
        task: Task,
        working_dir: str | None = None,
        positional_args: list[str] | None = None,
    ) -> str:
        """Wrap a shell command with sandbox isolation if configured.

        Returns the original command if no sandbox is active.
        """
        return build_sandbox_command(
            cmd,
            task,
            global_sandbox=self.pf.settings.sandbox,
            cwd=working_dir,
            positional_args=positional_args,
        )

    def _task_positional_args(
        self,
        task: Task,
        args: dict[str, str] | None,
    ) -> list[str]:
        """Return ordered shell positional arguments when the setting is enabled."""
        enabled = (
            task.options.positional_arguments
            if task.options.positional_arguments is not None
            else self.pf.settings.positional_arguments
        )
        if not enabled:
            return []
        values = args or {}
        return [values.get(argument.name, argument.default or "") for argument in task.arguments]

    @staticmethod
    def _resolved_task_args(
        task: Task,
        args: dict[str, str] | None,
    ) -> dict[str, str]:
        """Fill task argument defaults for shell positional/env behavior."""
        values = dict(args or {})
        for argument in task.arguments:
            if argument.name not in values and argument.default is not None:
                values[argument.name] = argument.default
        return values

    def _task_environment(
        self,
        task: Task,
        task_args: dict[str, str] | None,
    ) -> dict[str, str]:
        """Resolve explicit task environment values without invoking an LLM."""
        context = self.pf._build_context(
            task.name,
            task_args,
            promptfile_path=self.promptfile_path,
        )
        resolved: dict[str, str] = {}
        for name, raw_value in task.options.env.items():
            value = self.pf._interpolate(raw_value, context)
            value = self.pf._resolve_env_vars(value)
            value = self.pf._resolve_secrets(
                value,
                task,
                promptfile_path=self.promptfile_path,
                secret_callback=self._register_secret_value,
            )
            resolved[name] = value
        self._register_named_secret_values(resolved)
        return resolved

    def _run_shell_step(
        self,
        step: TaskStep,
        task: Task | None = None,
        working_dir: str | None = None,
        task_args: dict[str, str] | None = None,
        positional_args: list[str] | None = None,
    ) -> StepResult:
        """Execute a shell command locally."""
        shell_exe = self.pf.settings.shell or shutil.which("bash") or "/bin/bash"
        cmd = step.content
        sandboxed = bool(
            task and (task.options.sandbox or self.pf.settings.sandbox) not in (None, "none")
        )

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
            try:
                cmd = self._sandbox_command(
                    cmd,
                    task,
                    working_dir,
                    positional_args,
                )
            except ValueError as e:
                return StepResult(
                    kind="shell",
                    content=self._redact(step.content),
                    response=f"error: {e}",
                    success=False,
                    exit_code=2,
                )

        # Build environment with exported vars + makethlm defaults
        env = dict(os.environ)
        exported = self.pf.get_exported_env()
        if exported:
            env.update(exported)
        if task and task.options.env:
            env.update(self._task_environment(task, task_args))
        if task and task.options.env_enabled:
            env.update(task_args or {})

        # Inject MAKETHLM_* env vars
        if task:
            env["MAKETHLM_TASK"] = task.name
        if self.promptfile_path:
            env["MAKETHLM_FILE"] = self.promptfile_path
            env["MAKETHLM_DIR"] = os.path.dirname(os.path.abspath(self.promptfile_path))
        env.setdefault("HOME", os.path.expanduser("~"))

        try:
            timeout = self._shell_timeout(task)
            task_name = task.name if task else "makethlm"
            shell_args = positional_args or []
            tempdir = self.pf.settings.tempdir
            if tempdir:
                tempdir = os.path.abspath(os.path.expandvars(os.path.expanduser(tempdir)))
            if step.script:
                suffix = f".{task.options.extension}" if task and task.options.extension else ".sh"
                with tempfile.NamedTemporaryFile(
                    "w",
                    delete=False,
                    prefix="makethlm-",
                    suffix=suffix,
                    dir=tempdir,
                ) as script_file:
                    script_file.write(cmd)
                    script_file.write("\n")
                    script_path = script_file.name
                os.chmod(script_path, 0o700)
                try:
                    first_line = cmd.splitlines()[0] if cmd.splitlines() else ""
                    if task and task.options.script_command:
                        script_cmd: str | list[str] = [
                            *shlex.split(task.options.script_command),
                            script_path,
                            *shell_args,
                        ]
                    elif first_line.startswith("#!"):
                        script_cmd = [script_path, *shell_args]
                    else:
                        script_cmd = [shell_exe, script_path, *shell_args]
                    proc = _run_subprocess(
                        script_cmd,
                        shell=False,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        cwd=working_dir,
                        env=env,
                    )
                finally:
                    try:
                        os.unlink(script_path)
                    except OSError:
                        pass
            elif sandboxed:
                proc = _run_subprocess(
                    shlex.split(cmd),
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir,
                    env=env,
                )
            elif self.pf.settings.shell_argv:
                proc = _run_subprocess(
                    [
                        *self.pf.settings.shell_argv,
                        cmd,
                        task_name,
                        *shell_args,
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir,
                    env=env,
                )
            else:
                proc = _run_subprocess(
                    [shell_exe, "-c", cmd, task_name, *shell_args],
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=working_dir,
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
        except OSError as e:
            return StepResult(
                kind="shell",
                content=self._redact(step.content),
                response=f"error: could not execute command: {e}",
                success=step.ignore_error,
                exit_code=1,
            )

    def _run_ssh_step(self, step: TaskStep, host: str, group: HostGroup) -> StepResult:
        """Execute a shell command on a remote host via SSH."""
        result = run_ssh_command(
            host,
            step.content,
            group,
            ignore_error=step.ignore_error,
            silent=step.silent,
            build_command=_build_ssh_command,
            run_process=_run_subprocess,
        )
        return StepResult(
            kind="ssh",
            content=self._redact(step.content),
            response=self._redact(result.response),
            success=result.success,
            host=host,
            exit_code=result.exit_code,
        )

    def _dispatch_limited(
        self,
        dispatcher: Dispatcher,
        prompt: str,
        task: Task,
        provider_name: str,
    ) -> DispatchResult:
        """Dispatch under the provider's concurrency limit, if it declares one."""
        limiter = self._provider_limiter(provider_name)
        if limiter is None:
            return dispatcher.dispatch(prompt, task)
        with limiter:
            return dispatcher.dispatch(prompt, task)

    def _provider_limiter(self, provider_name: str) -> threading.Semaphore | None:
        """Return the shared semaphore for a provider that caps concurrency."""
        provider = self.pf.llm_providers.get(provider_name)
        if provider is None or not provider.max_concurrency:
            return None
        with self._limiter_lock:
            limiter = self._provider_limiters.get(provider_name)
            if limiter is None:
                limiter = threading.Semaphore(provider.max_concurrency)
                self._provider_limiters[provider_name] = limiter
            return limiter

    def _dispatch_prompt(
        self,
        dispatcher: Dispatcher,
        prompt: str,
        task: Task,
        provider_name: str,
        kind: str = "prompt",
    ) -> DispatchResult:
        """Dispatch a prompt, then record its usage against the run budget."""
        budget_error = self._budget_error(task)
        if budget_error:
            refusal = DispatchResult(response=budget_error, success=False)
            self._record_call(
                task=task,
                provider_name=provider_name,
                prompt=prompt,
                result=refusal,
                duration_ms=0,
                kind="budget",
            )
            return refusal

        with ElapsedIndicator(f"waiting on {provider_name}", enabled=self.verbose):
            started = time.monotonic()
        result = self._dispatch_with_fixtures(dispatcher, prompt, task, provider_name)
        duration_ms = int((time.monotonic() - started) * 1000)
        if result.cost_usd is None:
            result.cost_usd = derive_cost(
                self.pf.llm_providers.get(provider_name),
                result.tokens_in,
                result.tokens_out,
            )
        with self._cost_lock:
            self.costs.add(result.tokens_in, result.tokens_out, result.cost_usd)
        self._record_call(
            task=task,
            provider_name=provider_name,
            prompt=prompt,
            result=result,
            duration_ms=duration_ms,
            kind=kind,
        )
        return result

    def _record_call(
        self,
        *,
        task: Task,
        provider_name: str,
        prompt: str,
        result: DispatchResult,
        duration_ms: int,
        kind: str,
    ) -> None:
        """Append one dispatch attempt to the call log, if one is configured."""
        if self.call_log is None:
            return
        source = "provider"
        if self.fixtures is not None and not self.record_fixtures:
            source = "fixture"
        self.call_log.record(
            CallRecord(
                task=task.name,
                provider=provider_name,
                kind=kind,
                attempt=self._next_call_index(),
                success=result.success,
                duration_ms=duration_ms,
                prompt=self._redact(prompt),
                response=self._redact(result.response),
                source=source,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_usd=result.cost_usd,
            )
        )

    def _next_call_index(self) -> int:
        """Return a monotonically increasing index across concurrent calls."""
        with self._cost_lock:
            self._call_index += 1
            return self._call_index

    def _budget_error(self, task: Task) -> str | None:
        """Return an error when the run has already spent its budget."""
        budget = self._task_budget(task)
        if budget is None:
            return None
        with self._cost_lock:
            spent = self.costs.cost_usd
        if spent < budget:
            return None
        return (
            f"budget exceeded: spent ${spent:.4f} of the ${budget:.4f} limit "
            f"before task {task.name!r} could dispatch another prompt"
        )

    def _task_budget(self, task: Task) -> float | None:
        """Return the effective budget for a task, if any."""
        budgets = [
            value
            for value in (self.max_cost, _parse_task_cost(task.options.max_cost))
            if value is not None
        ]
        return min(budgets) if budgets else None

    def _dispatch_with_fixtures(
        self,
        dispatcher: Dispatcher,
        prompt: str,
        task: Task,
        provider_name: str,
    ) -> DispatchResult:
        """Dispatch a prompt, serving or recording a fixture when configured."""
        if self.fixtures is None:
            return self._dispatch_limited(dispatcher, prompt, task, provider_name)

        key_prompt = self._redact(prompt)
        if not self.record_fixtures:
            fixture = self.fixtures.load(task.name, key_prompt)
            if fixture is None:
                return DispatchResult(
                    response=(
                        f"no recorded fixture for a prompt in task {task.name!r}; "
                        "record one with --fixtures DIR --record-fixtures"
                    ),
                    success=False,
                )
            return DispatchResult(
                response=str(fixture["response"]),
                success=bool(fixture.get("success", True)),
                # A replayed fixture costs nothing.
                cost_usd=0.0,
            )

        result = self._dispatch_limited(dispatcher, prompt, task, provider_name)
        self.fixtures.save(
            task.name,
            key_prompt,
            self._redact(result.response),
            success=result.success,
            provider=provider_name,
        )
        return result

    def _attempt_with_provider(
        self,
        prompt: str,
        task: Task,
        provider_name: str,
        dispatcher: Dispatcher,
        output_contract: str | None,
        kind: str = "prompt",
    ) -> tuple[DispatchResult, int]:
        """Run one provider's retry and repair loop; return its result and attempts."""
        retries = min(max(task.options.retries, 0), MAX_LLM_RETRIES)
        repair_budget = (
            min(max(task.options.repair, 0), MAX_REPAIR_ATTEMPTS) if output_contract else 0
        )
        repairs_used = 0
        attempts = 0
        dispatch_result = DispatchResult(response="", success=False)

        for provider_attempt in range(1, retries + 2):
            attempts += 1
            if self.verbose and provider_attempt > 1:
                _log(
                    f"         retrying prompt with {provider_name} (attempt {provider_attempt})",
                    dim=True,
                )
            dispatch_result = self._dispatch_prompt(
                dispatcher, prompt, task, provider_name, kind=kind
            )
            if not dispatch_result.success and is_rate_limited(dispatch_result.response):
                delay = rate_limit_backoff(provider_attempt)
                if self.verbose:
                    _log(
                        f"         {provider_name} is rate limited; "
                        f"waiting {delay:.0f}s before retrying",
                        dim=True,
                    )
                time.sleep(delay)
            # Re-prompt while the response violates the task's output contract.
            while (
                dispatch_result.success
                and output_contract
                and repairs_used < repair_budget
                and not value_matches(dispatch_result.response, output_contract)
            ):
                repairs_used += 1
                attempts += 1
                if self.verbose:
                    _log(
                        f"         output is not {output_contract}; "
                        f"repairing (attempt {repairs_used})",
                        dim=True,
                    )
                dispatch_result = self._dispatch_prompt(
                    dispatcher,
                    build_repair_prompt(prompt, output_contract, dispatch_result.response),
                    task,
                    provider_name,
                    kind="repair",
                )
            if dispatch_result.success:
                break
        return dispatch_result, attempts

    def _step_dispatchers(
        self,
        step: TaskStep,
        task: Task,
        dispatcher: Dispatcher,
    ) -> list[tuple[str, Dispatcher]]:
        """Return the providers a prompt step should be sent to, in order.

        A step-level ``@llm`` wins, then a task-level fan-out list, then the
        task's single provider.
        """
        if step.llm:
            provider = self.pf.llm_providers.get(step.llm)
            if provider is not None:
                return [(step.llm, _dispatcher_for_provider(provider))]

        agent = self.pf.get_agent_for_task(task.name)
        if not (agent and agent.llm) and len(task.options.llms) > 1:
            targets: list[tuple[str, Dispatcher]] = []
            for name in task.options.llms:
                provider = self.pf.llm_providers.get(name)
                if provider is not None:
                    targets.append((name, _dispatcher_for_provider(provider)))
            if targets:
                return targets

        primary_name = agent.llm if agent and agent.llm else task.options.llm or self.pf.default_llm
        return [(primary_name or type(dispatcher).__name__, dispatcher)]

    def _run_prompt_step(
        self,
        step: TaskStep,
        task: Task,
        dispatcher: Dispatcher,
        runtime_context: dict[str, str] | None = None,
        pipe_context: str | None = None,
        output_contract: str | None = None,
    ) -> StepResult:
        """Send a prompt step to the LLM dispatcher.

        A task with ``llm="a|b"`` sends the prompt to every provider at once and
        keeps each answer; ``judge`` then merges them. Otherwise the single
        provider is tried, falling back through ``fallback-llm`` on failure.
        When *output_contract* is set, a response that violates it is
        re-prompted up to the task's ``repair`` budget.
        """
        prompt = step.content
        if runtime_context:
            prompt = self.pf._interpolate(prompt, runtime_context)
        if pipe_context:
            prompt = f"{pipe_context}\n\n{prompt}"

        targets = self._step_dispatchers(step, task, dispatcher)
        if len(targets) > 1:
            return self._run_fanout_step(prompt, task, targets, output_contract)

        primary_label, primary_dispatcher = targets[0]
        candidates: list[tuple[str, Dispatcher]] = [(primary_label, primary_dispatcher)]
        seen_providers = {primary_label}
        for provider_name in task.options.fallback_llms:
            provider = self.pf.llm_providers.get(provider_name)
            if (
                provider
                and provider_name not in seen_providers
                and len(candidates) <= MAX_FALLBACK_LLMS
            ):
                candidates.append((provider_name, _dispatcher_for_provider(provider)))
                seen_providers.add(provider_name)

        total_attempt = 0
        last_response = ""
        last_provider: str | None = None
        for provider_name, candidate in candidates:
            last_provider = provider_name
            dispatch_result, attempts = self._attempt_with_provider(
                prompt, task, provider_name, candidate, output_contract
            )
            total_attempt += attempts
            last_response = dispatch_result.response
            if dispatch_result.success:
                return StepResult(
                    kind="prompt",
                    content=self._redact(prompt),
                    response=self._redact(last_response),
                    success=True,
                    provider=provider_name,
                    attempt=total_attempt,
                )

        return StepResult(
            kind="prompt",
            content=self._redact(prompt),
            response=self._redact(last_response),
            success=False,
            provider=last_provider,
            attempt=total_attempt,
        )

    def _run_fanout_step(
        self,
        prompt: str,
        task: Task,
        targets: list[tuple[str, Dispatcher]],
        output_contract: str | None,
    ) -> StepResult:
        """Send one prompt to several providers at once and keep every answer.

        The step succeeds when at least one provider answered, matching how
        ``fallback-llm`` treats a working provider as enough. Failed providers
        are still recorded so the outcome is inspectable.
        """
        if self.verbose:
            names = ", ".join(name for name, _ in targets)
            _log(f"         > fanning out to {names}", dim=True)

        results: dict[str, tuple[DispatchResult, int]] = {}
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = {
                pool.submit(
                    self._attempt_with_provider,
                    prompt,
                    task,
                    name,
                    candidate,
                    output_contract,
                    "fanout",
                ): name
                for name, candidate in targets
            }
            for future in futures:
                results[futures[future]] = future.result()

        ordered = [(name, results[name][0]) for name, _ in targets if name in results]
        variants = {name: self._redact(outcome.response) for name, outcome in ordered}
        succeeded = [(name, outcome) for name, outcome in ordered if outcome.success]
        total_attempt = sum(results[name][1] for name, _ in ordered)

        if not succeeded:
            return StepResult(
                kind="prompt",
                content=self._redact(prompt),
                response=self._redact(format_fanout_response(ordered)),
                success=False,
                provider="|".join(name for name, _ in ordered),
                attempt=total_attempt,
                variants=variants,
            )

        if task.options.judge:
            judged = self._run_judge(prompt, task, succeeded, output_contract)
            if judged is not None:
                judged.variants = variants
                judged.attempt = (judged.attempt or 0) + total_attempt
                return judged

        return StepResult(
            kind="prompt",
            content=self._redact(prompt),
            response=self._redact(format_fanout_response(ordered)),
            success=True,
            provider="|".join(name for name, _ in ordered),
            attempt=total_attempt,
            variants=variants,
        )

    def _run_judge(
        self,
        prompt: str,
        task: Task,
        answers: list[tuple[str, DispatchResult]],
        output_contract: str | None,
    ) -> StepResult | None:
        """Merge fan-out answers with the judge provider.

        Returns ``None`` when the judge is not usable, so the caller falls back
        to reporting every answer rather than losing them.
        """
        judge_name = task.options.judge
        if not judge_name:
            return None
        provider = self.pf.llm_providers.get(judge_name)
        if provider is None:
            return None
        if self.verbose:
            _log(f"         > merging {len(answers)} answers with {judge_name}", dim=True)
        judge_prompt = build_judge_prompt(prompt, [(name, r.response) for name, r in answers])
        outcome, attempts = self._attempt_with_provider(
            judge_prompt,
            task,
            judge_name,
            _dispatcher_for_provider(provider),
            output_contract,
            kind="judge",
        )
        if not outcome.success:
            return None
        return StepResult(
            kind="prompt",
            content=self._redact(judge_prompt),
            response=self._redact(outcome.response),
            success=True,
            provider=judge_name,
            attempt=attempts,
        )

    def _run_docker_task(
        self,
        task: Task,
        resolved_steps: list[TaskStep],
        prompt_sent: str,
    ) -> TaskResult:
        """Handle a docker block: generate Dockerfile via LLM, then build."""
        docker = task.docker
        if docker is None:
            raise ValueError(f"task {task.name!r} is not a docker task")
        step_results: list[StepResult] = []
        dispatcher = self._get_dispatcher(task)

        generate_prompt = docker_generate_prompt(resolved_steps)

        if self.verbose:
            _log("         > generating Dockerfile via LLM ...", dim=True)
        if self.dry_run:
            step_results.append(
                StepResult(
                    kind="docker-generate",
                    content=self._redact(generate_prompt),
                    response="[dry-run] generate Dockerfile",
                    success=True,
                )
            )
            dry_run_command = docker_dry_run_build_command(
                task.name, docker.tag, docker.context, docker.file
            )
            step_results.append(
                StepResult(
                    kind="docker-build",
                    content=dry_run_command,
                    response="[dry-run] build Docker image",
                    success=True,
                )
            )
            return TaskResult(
                task_name=task.name,
                prompt_sent=self._redact(prompt_sent),
                response="\n".join(sr.response for sr in step_results),
                success=True,
                step_results=step_results,
            )

        dr = dispatcher.dispatch(generate_prompt, task)
        step_results.append(
            StepResult(
                kind="docker-generate",
                content=self._redact(generate_prompt),
                response=self._redact(dr.response),
                success=dr.success,
            )
        )

        if not dr.success:
            return TaskResult(
                task_name=task.name,
                prompt_sent=self._redact(prompt_sent),
                response=self._redact(dr.response),
                success=False,
                step_results=step_results,
            )

        try:
            context_path, dockerfile_path = _resolve_dockerfile_path(docker.context, docker.file)
        except ValueError as e:
            step_results.append(
                StepResult(
                    kind="docker-build",
                    content=f"write {docker.file}",
                    response=f"error writing Dockerfile: {e}",
                    success=False,
                )
            )
            return TaskResult(
                task_name=task.name,
                prompt_sent=self._redact(prompt_sent),
                response=f"error writing Dockerfile: {e}",
                success=False,
                step_results=step_results,
            )
        dockerfile_content = strip_dockerfile_markdown_fence(dr.response)

        try:
            os.makedirs(dockerfile_path.parent, exist_ok=True)
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_content + "\n")
        except OSError as e:
            step_results.append(
                StepResult(
                    kind="docker-build",
                    content=f"write {dockerfile_path}",
                    response=f"error writing Dockerfile: {e}",
                    success=False,
                )
            )
            return TaskResult(
                task_name=task.name,
                prompt_sent=self._redact(prompt_sent),
                response=f"error writing Dockerfile: {e}",
                success=False,
                step_results=step_results,
            )

        build_cmd = docker_build_argv(task.name, docker.tag, dockerfile_path, context_path)
        build_execution = run_docker_build(
            build_cmd,
            self._shell_timeout(task),
            run_process=_run_subprocess,
        )
        build_result = StepResult(
            kind="docker-build",
            content=self._redact(format_docker_build_command(build_cmd)),
            response=self._redact(build_execution.response),
            success=build_execution.success,
            exit_code=build_execution.exit_code,
        )
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

        # Collect stdout from shell steps. stderr is mixed into stdout today.
        stdout_parts: list[str] = []
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

        artifact: dict[str, str] = {
            "stdout": "\n".join(stdout_parts).strip(),
            "stderr": "",  # stderr is mixed into stdout in current implementation
            "exit_code": last_exit_code if not task_result.success else "0",
            "success": "true" if task_result.success else "false",
            "response": "\n".join(response_parts).strip(),
        }
        # Fan-out answers stay individually addressable as {{task.provider.response}}.
        for sr in task_result.step_results:
            for provider_name, answer in sr.variants.items():
                artifact[f"{provider_name}.response"] = answer
        self.artifacts[artifact_name] = artifact

    def _store_skipped_artifact(self, task: Task) -> None:
        """Record a placeholder artifact for a task that was skipped."""
        artifact_name = task.options.register or task.name
        self.artifacts[artifact_name] = {
            "stdout": "",
            "stderr": "",
            "exit_code": "0",
            "success": "skipped",
            "response": "",
        }

    def _fire_webhook(self, task: Task, task_result: TaskResult, elapsed: float) -> None:
        """Send a webhook notification if configured."""
        error = send_webhook(
            task,
            task_result,
            elapsed,
            redact=self._redact,
            request_factory=urllib.request.Request,
            urlopen=urllib.request.urlopen,
        )
        if error and self.verbose:
            _log(f"         webhook failed: {error}", dim=True)
