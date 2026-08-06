"""Secret backend resolution and masking helpers."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from .subprocess_util import run_subprocess

SECRET_NAME_RE = re.compile(
    r"(?:^|[_-])(?:SECRET|TOKEN|PASSWORD|PASS|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"CREDENTIALS?|KEY|AUTH(?:ORIZATION)?|COOKIE|SESSION|DATABASE[_-]?URL)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)


class SecretError(Exception):
    """Raised when a configured secrets backend cannot resolve a value."""


def _run_secret_command(cmd: list[str], backend: str) -> str:
    """Run a secret backend with bounded, non-disclosing failure handling."""
    try:
        proc = run_subprocess(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SecretError(f"secret backend tool not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise SecretError(f"{backend} secret resolution timed out")
    if proc.returncode != 0:
        raise SecretError(f"{backend} failed to resolve secret")
    return proc.stdout.strip()


def is_secret_name(name: str) -> bool:
    """Return whether a variable name is likely to contain a secret."""
    return bool(SECRET_NAME_RE.search(name))


def secret_values_from_mapping(values: dict[str, str]) -> set[str]:
    """Return redactable secret-like values from a named mapping."""
    return {value for name, value in values.items() if is_secret_name(name) and len(value) >= 3}


def redact_text(text: str, secret_values: list[str] | set[str]) -> str:
    """Replace known secret values in text, longest values first."""
    redacted = text
    for value in sorted(secret_values, key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def redact_named_values(values: dict[str, str]) -> dict[str, str]:
    """Mask values whose names look secret-bearing."""
    return {
        name: "[redacted]" if is_secret_name(name) and value else value
        for name, value in values.items()
    }


def secret_backend_for_task(settings: Any, task: Any) -> str:
    """Return the secrets backend for a task."""
    return task.options.secrets or settings.secrets or "env"


def resolve_secret(
    settings: Any,
    secret_ref: str,
    task: Any,
    *,
    promptfile_path: str | None = None,
) -> str:
    """Resolve a single ``{{#secret:...}}`` reference."""
    backend = secret_backend_for_task(settings, task)
    ref = secret_ref.strip()

    if backend == "env":
        value = os.environ.get(ref)
        if value is None and "/" in ref:
            value = os.environ.get(ref.replace("/", "_"))
        if value is None:
            raise SecretError(f"secret not found in environment: {ref!r}")
        return value

    if backend == "infisical":
        cmd = ["infisical", "secrets", "get", ref, "--plain"]
        if settings.secrets_project:
            cmd.append(f"--projectId={settings.secrets_project}")
        if settings.secrets_environment:
            cmd.append(f"--env={settings.secrets_environment}")
        return _run_secret_command(cmd, backend)

    if backend == "1password":
        vault = settings.secrets_vault
        if "/" in ref:
            op_path = ref
        elif vault:
            op_path = f"{vault}/{ref}"
        else:
            raise SecretError("1password secrets require set secrets-vault or a full op:// path")
        cmd = ["op", "read", f"op://{op_path}"]
        return _run_secret_command(cmd, backend)

    if backend == "sops":
        secrets_file = settings.secrets_file
        if not secrets_file:
            raise SecretError("sops secrets require set secrets-file")
        base_dir = (
            os.path.dirname(os.path.abspath(promptfile_path)) if promptfile_path else os.getcwd()
        )
        resolved_file = os.path.normpath(os.path.join(base_dir, secrets_file))
        extract = "".join(f'["{part}"]' for part in ref.split("/"))
        cmd = ["sops", "decrypt", "--extract", extract, resolved_file]
        return _run_secret_command(cmd, backend)

    raise SecretError(f"unknown secrets backend: {backend!r}")


def audit_secret_resolution(settings: Any, secret_ref: str, task: Any) -> None:
    """Log secret resolution metadata without exposing the resolved value."""
    if not settings.secrets_audit:
        return
    backend = secret_backend_for_task(settings, task)
    print(
        f"makethlm: resolved secret {secret_ref!r} via {backend!r} for task {task.name!r}",
        file=sys.stderr,
    )


def resolve_secrets(
    text: str,
    settings: Any,
    task: Any,
    *,
    promptfile_path: str | None = None,
    mask_only: bool = False,
    secret_callback: Callable[[str], None] | None = None,
) -> str:
    """Resolve ``{{#secret:NAME}}`` placeholders in text."""

    def _replace(m: re.Match[str]) -> str:
        secret_ref = m.group(1).strip()
        if mask_only:
            return "***"
        value = resolve_secret(settings, secret_ref, task, promptfile_path=promptfile_path)
        audit_secret_resolution(settings, secret_ref, task)
        if secret_callback:
            secret_callback(value)
        return value

    return re.sub(r"\{\{#secret:(.+?)\}\}", _replace, text)
