"""Sandbox command construction helpers."""

from __future__ import annotations

import os
import shlex

from .models import Task


def quote_command(parts: list[str]) -> str:
    """Return a shell-escaped command string."""
    return " ".join(shlex.quote(part) for part in parts)


def build_sandbox_command(
    cmd: str,
    task: Task,
    *,
    global_sandbox: str | None = None,
    cwd: str | None = None,
    positional_args: list[str] | None = None,
) -> str:
    """Wrap a command with the configured sandbox command."""
    sandbox = task.options.sandbox or global_sandbox
    if not sandbox or sandbox == "none":
        return cmd

    if sandbox == "docker":
        if task.options.sandbox_net not in (None, "none", "host"):
            raise ValueError(f"unsupported sandbox network mode: {task.options.sandbox_net!r}")
        workspace = os.path.abspath(cwd or os.getcwd())
        image = task.options.sandbox_image or "ubuntu:latest"
        workspace_mount = f"{workspace}:/workspace"
        if task.options.sandbox_read_only:
            workspace_mount += ":ro"
        parts = ["docker", "run", "--rm", "-v", workspace_mount, "-w", "/workspace"]
        if task.options.sandbox_mount:
            parts.extend(["-v", task.options.sandbox_mount])
        parts.extend(["--net", task.options.sandbox_net or "none"])
        parts.append(image)
        parts.extend(["sh", "-c", cmd, task.name, *(positional_args or [])])
        return quote_command(parts)

    if sandbox == "systemd":
        if task.options.sandbox_net not in (None, "none", "host"):
            raise ValueError(f"unsupported sandbox network mode: {task.options.sandbox_net!r}")
        workspace = os.path.abspath(cwd or os.getcwd())
        parts = [
            "systemd-run",
            "--scope",
            "--quiet",
            "--property=PrivateTmp=yes",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
        ]
        if task.options.sandbox_net in (None, "none"):
            parts.append("--property=PrivateNetwork=yes")
        if task.options.sandbox_read_only:
            parts.append(f"--property=ReadOnlyPaths={workspace}")
        else:
            parts.append(f"--property=ReadWritePaths={workspace}")
        parts.extend(["sh", "-c", cmd, task.name, *(positional_args or [])])
        return quote_command(parts)

    if sandbox == "bwrap":
        if task.options.sandbox_net not in (None, "none", "host"):
            raise ValueError(f"unsupported sandbox network mode: {task.options.sandbox_net!r}")
        workspace = os.path.abspath(cwd or os.getcwd())
        bind_flag = "--ro-bind" if task.options.sandbox_read_only else "--bind"
        parts = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        if task.options.sandbox_net in (None, "none"):
            parts.append("--unshare-net")
        parts.extend(
            [
                bind_flag,
                workspace,
                workspace,
                "sh",
                "-c",
                cmd,
                task.name,
                *(positional_args or []),
            ]
        )
        return quote_command(parts)

    raise ValueError(f"unknown sandbox backend: {sandbox!r}")
