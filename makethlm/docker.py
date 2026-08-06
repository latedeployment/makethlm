"""Docker task helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import TaskStep
from .subprocess_util import run_subprocess as _run_subprocess

DOCKER_GENERATE_PREFIX = (
    "Generate a Dockerfile based on the following description. "
    "Output ONLY the raw Dockerfile content — no markdown fences, "
    "no explanation, no commentary. Just the Dockerfile.\n\n"
)


@dataclass(frozen=True)
class DockerBuildExecutionResult:
    """Raw result of running docker build."""

    response: str
    success: bool
    exit_code: int


def docker_generate_prompt(steps: list[TaskStep]) -> str:
    """Build the LLM prompt used to generate a Dockerfile."""
    description = "\n".join(step.content for step in steps if step.kind == "prompt")
    return DOCKER_GENERATE_PREFIX + description


def docker_dry_run_build_command(task_name: str, tag: str, context: str, dockerfile: str) -> str:
    """Return the displayed docker build command for dry runs."""
    return f"docker build -t {task_name}:{tag} -f {os.path.join(context, dockerfile)} {context}"


def resolve_dockerfile_path(context: str, dockerfile: str) -> tuple[Path, Path]:
    """Return resolved context and Dockerfile paths, rejecting path escapes."""
    dockerfile_part = Path(dockerfile)
    if dockerfile_part.is_absolute():
        raise ValueError("docker file must be relative to docker context")
    context_path = Path(context).expanduser().resolve()
    dockerfile_path = (context_path / dockerfile_part).resolve()
    try:
        dockerfile_path.relative_to(context_path)
    except ValueError:
        raise ValueError("docker file must stay inside docker context")
    return context_path, dockerfile_path


def strip_dockerfile_markdown_fence(content: str) -> str:
    """Remove a single surrounding markdown code fence from Dockerfile content."""
    dockerfile_content = content.strip()
    if not dockerfile_content.startswith("```"):
        return dockerfile_content
    lines = dockerfile_content.split("\n")
    if lines[-1].strip() == "```":
        lines = lines[1:-1]
    else:
        lines = lines[1:]
    return "\n".join(lines)


def docker_build_argv(
    task_name: str, tag: str, dockerfile_path: Path, context_path: Path
) -> list[str]:
    """Build argv for `docker build`."""
    return [
        "docker",
        "build",
        "-t",
        f"{task_name}:{tag}",
        "-f",
        str(dockerfile_path),
        str(context_path),
    ]


def format_docker_build_command(argv: list[str]) -> str:
    """Return a shell-escaped docker build command for display."""
    return " ".join(shlex.quote(part) for part in argv)


def run_docker_build(
    argv: list[str],
    timeout: float,
    *,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = _run_subprocess,
) -> DockerBuildExecutionResult:
    """Run docker build and normalize timeout/process output."""
    try:
        proc = run_process(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return DockerBuildExecutionResult(
            response=f"error: command timed out after {timeout:.1f}s",
            success=False,
            exit_code=124,
        )

    output = proc.stdout
    if proc.stderr:
        output += proc.stderr
    return DockerBuildExecutionResult(
        response=output.strip(),
        success=proc.returncode == 0,
        exit_code=proc.returncode,
    )
