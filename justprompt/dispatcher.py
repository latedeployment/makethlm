"""LLM dispatcher interface and implementations."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import Task


@dataclass
class DispatchResult:
    """Result from dispatching a prompt to an LLM."""

    response: str
    success: bool


class Dispatcher(ABC):
    """Abstract base for LLM dispatchers."""

    @abstractmethod
    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        """Send a prompt to an LLM and return the result."""


class DryRunDispatcher(Dispatcher):
    """Records prompts without calling any LLM. Useful for testing."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, Task]] = []

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        self.dispatched.append((prompt, task))
        return DispatchResult(response=f"[dry-run] {task.name}: {prompt}", success=True)


class ClaudeDispatcher(Dispatcher):
    """Dispatches prompts to the Claude CLI (`claude -p`)."""

    def __init__(self, model: str | None = None):
        self.default_model = model

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        model = task.options.model or self.default_model
        cmd = ["claude", "-p", prompt]
        if model:
            cmd.extend(["--model", model])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return DispatchResult(
                response=result.stdout,
                success=result.returncode == 0,
            )
        except FileNotFoundError:
            return DispatchResult(
                response="error: 'claude' CLI not found on PATH",
                success=False,
            )
        except subprocess.TimeoutExpired:
            return DispatchResult(
                response="error: claude CLI timed out after 300s",
                success=False,
            )


class ShellDispatcher(Dispatcher):
    """Dispatches prompts to any LLM CLI via a configurable shell template.

    Example template: 'openai chat -m gpt-4 -p "{prompt}"'
    The {prompt} placeholder is replaced with the actual prompt.
    """

    def __init__(self, template: str):
        self.template = template

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        # Escape single quotes in the prompt for safe shell interpolation
        safe_prompt = prompt.replace("'", "'\\''")
        cmd = self.template.replace("{prompt}", safe_prompt)

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
            )
            return DispatchResult(
                response=result.stdout,
                success=result.returncode == 0,
            )
        except subprocess.TimeoutExpired:
            return DispatchResult(
                response=f"error: command timed out after 300s",
                success=False,
            )
