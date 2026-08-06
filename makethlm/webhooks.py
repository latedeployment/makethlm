"""Webhook notification helpers."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .models import Task


def should_send_webhook(webhook_on: str | None, success: bool) -> bool:
    """Return whether a webhook should fire for the task result."""
    if webhook_on == "success" and not success:
        return False
    if webhook_on == "failure" and success:
        return False
    return True


def send_webhook(
    task: Task,
    task_result: Any,
    elapsed: float,
    *,
    redact: Callable[[str], str],
    request_factory: Callable[..., urllib.request.Request] = urllib.request.Request,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> str | None:
    """Send a task webhook and return an error string if delivery fails."""
    webhook_url = task.options.webhook
    if not webhook_url:
        return None
    if not should_send_webhook(task.options.webhook_on, task_result.success):
        return None

    status = "success" if task_result.success else "failure"
    stdout = redact(task_result.response)[:4096]
    payload = {
        "task": task.name,
        "status": status,
        "exit_code": 0 if task_result.success else 1,
        "stdout": stdout,
        "duration_ms": int(elapsed * 1000),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        preset = None
        url = webhook_url
        if ":" in webhook_url:
            maybe_preset, maybe_url = webhook_url.split(":", 1)
            if maybe_preset in ("ntfy", "gotify", "discord", "slack") and maybe_url.startswith(
                ("http://", "https://")
            ):
                preset = maybe_preset
                url = maybe_url

        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            return "webhook delivery failed"

        if preset == "ntfy":
            body = f"{task.name}: {status}\n{stdout}".encode()
            req = request_factory(
                url,
                data=body,
                headers={
                    "Title": f"makethlm {task.name}",
                    "Tags": "white_check_mark" if task_result.success else "warning",
                },
                method="POST",
            )
        elif preset == "gotify":
            data = json.dumps(
                {
                    "title": f"makethlm {task.name}: {status}",
                    "message": stdout,
                    "priority": 5 if task_result.success else 8,
                }
            ).encode("utf-8")
            req = request_factory(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        elif preset in ("discord", "slack"):
            data = json.dumps(
                {
                    "content" if preset == "discord" else "text": (
                        f"makethlm `{task.name}` {status}\n{stdout}"
                    )
                }
            ).encode("utf-8")
            req = request_factory(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            data = json.dumps(payload).encode("utf-8")
            req = request_factory(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        urlopen(req, timeout=10)
    except (urllib.error.URLError, OSError, ValueError):
        return "webhook delivery failed"

    return None
