"""Task runner with dependency resolution, SSH execution, and LLM routing."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .models import HostGroup, LLMProvider, Promptfile, Task, TaskStep
from .dispatcher import Dispatcher, DispatchResult, ClaudeDispatcher, ShellDispatcher


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


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Result of running a single step within a task."""

    kind: str  # "shell", "prompt", "docker-generate", "docker-build", "ssh"
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


def _load_dotenv(path: str | None = None) -> None:
    """Load a .env file into os.environ (simple key=value parser)."""
    env_path = Path(path) if path else Path(".env")
    if not env_path.is_file():
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

    def __init__(self, pf: Promptfile, dispatcher: Dispatcher):
        self.pf = pf
        self.dispatcher = dispatcher  # fallback/default dispatcher
        self._dotenv_loaded = False

    def _get_dispatcher(self, task: Task) -> Dispatcher:
        """Return the appropriate dispatcher for a task (per-task LLM > default)."""
        provider = self.pf.get_llm_for_task(task.name)
        if provider:
            return _dispatcher_for_provider(provider)
        return self.dispatcher

    def _ensure_dotenv(self) -> None:
        """Load .env if ``set dotenv-load`` is enabled and not yet loaded."""
        if not self._dotenv_loaded and self.pf.settings.dotenv_load:
            _load_dotenv()
            self._dotenv_loaded = True

    def _resolve_working_dir(self, task: Task) -> str | None:
        """Return effective working directory for a task."""
        return task.options.working_dir or self.pf.settings.working_dir

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

        for task_name in execution_order:
            task = self.pf.tasks[task_name]

            # OS filter: skip tasks not meant for this OS
            if task.options.should_skip_for_os():
                result.task_results.append(TaskResult(
                    task_name=task_name,
                    prompt_sent="",
                    response=f"[skipped] not applicable on this OS (requires {task.options.os_filter})",
                    success=True,
                ))
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

            task_args = args if task_name == target else None
            task_result = self._run_task(task, task_args)
            result.task_results.append(task_result)

            if not task_result.success:
                break

        return result

    def _run_task(self, task: Task, args: dict[str, str] | None) -> TaskResult:
        """Execute a single task's steps."""
        resolved_steps = self.pf.resolve_steps(task.name, args)
        prompt_sent = self.pf.resolve_prompt(task.name, args)

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
            if step.kind == "shell":
                sr = self._run_shell_step(step, working_dir=working_dir)
            else:
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
            if step.kind == "shell":
                # Execute on each host
                for host in host_group.hosts:
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

    def _run_shell_step(self, step: TaskStep, working_dir: str | None = None) -> StepResult:
        """Execute a shell command locally."""
        shell_exe = self.pf.settings.shell or None
        try:
            proc = subprocess.run(
                step.content,
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=working_dir,
                executable=shell_exe,
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
            proc = subprocess.run(
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
