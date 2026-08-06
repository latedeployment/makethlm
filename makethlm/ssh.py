"""SSH command construction, validation, and execution helpers."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from .models import HostGroup, parse_duration_seconds
from .subprocess_util import run_subprocess as _run_subprocess

_SSH_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SSH_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class SSHExecutionResult:
    """Raw result of executing one command through SSH."""

    response: str
    success: bool
    exit_code: int


def validate_ssh_part(label: str, value: str, pattern: re.Pattern[str]) -> None:
    """Validate an SSH target component before it becomes argv data."""
    if not value or value.startswith("-") or not pattern.fullmatch(value):
        raise ValueError(f"invalid SSH {label}: {value!r}")


def build_ssh_argv(host: str, command: str, group: HostGroup) -> list[str]:
    """Build SSH argv for remote execution."""
    validate_ssh_part("host", host, _SSH_HOST_RE)
    if group.user:
        validate_ssh_part("user", group.user, _SSH_USER_RE)
    parts = ["ssh"]
    if group.identity_file:
        identity_file = os.path.expandvars(os.path.expanduser(group.identity_file))
        parts.extend(["-i", identity_file])
    if group.port:
        parts.extend(["-p", str(group.port)])
    parts.append("-o")
    parts.append("BatchMode=yes")
    if group.strict_host_key_checking:
        parts.extend(["-o", f"StrictHostKeyChecking={group.strict_host_key_checking}"])
    target = f"{group.user}@{host}" if group.user else host
    parts.append(target)
    parts.append(command)
    return parts


def build_ssh_command(host: str, command: str, group: HostGroup) -> str:
    """Build a shell-escaped SSH command string for display/backward compatibility."""
    return " ".join(shlex.quote(part) for part in build_ssh_argv(host, command, group))


def _fmt_elapsed(seconds: float) -> str:
    return f"{seconds:.1f}s"


def run_ssh_command(
    host: str,
    command: str,
    group: HostGroup,
    *,
    ignore_error: bool = False,
    silent: bool = False,
    build_command: Callable[[str, str, HostGroup], str] = build_ssh_command,
    run_process: Callable[..., subprocess.CompletedProcess[str]] = _run_subprocess,
) -> SSHExecutionResult:
    """Execute a command on one host and normalize SSH errors into a result."""
    try:
        ssh_cmd = shlex.split(build_command(host, command, group))
    except ValueError as e:
        return SSHExecutionResult(
            response=f"error: {e}",
            success=ignore_error,
            exit_code=1,
        )

    timeout = parse_duration_seconds(group.timeout) if group.timeout else 120
    try:
        proc = run_process(
            ssh_cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return SSHExecutionResult(
            response=f"error: SSH to {host} timed out after {_fmt_elapsed(timeout)}",
            success=ignore_error,
            exit_code=124,
        )

    output = proc.stdout
    if proc.stderr:
        output += proc.stderr
    return SSHExecutionResult(
        response=output.strip() if not silent else "",
        success=proc.returncode == 0 or ignore_error,
        exit_code=proc.returncode,
    )
