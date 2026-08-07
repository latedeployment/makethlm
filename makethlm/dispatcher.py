"""LLM dispatcher interface and implementations."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .models import Task, parse_duration_seconds
from .subprocess_util import run_subprocess


@dataclass
class DispatchResult:
    """Result from dispatching a prompt to an LLM.

    Providers that report usage fill in the token counts; ``cost_usd`` is set
    only by providers that report spend directly. Otherwise the runner derives
    cost from the provider's declared prices.
    """

    response: str
    success: bool
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


def _int_or_none(value: object) -> int | None:
    """Return a non-negative int from provider usage data, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number >= 0 else None


def _extract_tool_name(template: str) -> str | None:
    """Extract the CLI tool name (first token) from a shell template.

    Returns ``None`` if the template is empty or blank.

    >>> _extract_tool_name('codex "{prompt}"')
    'codex'
    >>> _extract_tool_name('/usr/local/bin/codex "{prompt}"')
    'codex'
    """
    first_token = template.split()[0] if template.strip() else None
    if first_token is None:
        return None
    # For path-qualified commands like /usr/bin/codex, return just the basename
    return first_token.rsplit("/", 1)[-1]


class Dispatcher(ABC):
    """Abstract base for LLM dispatchers."""

    @abstractmethod
    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        """Send a prompt to an LLM and return the result."""

    def validate_tool(self) -> str | None:
        """Check that the required CLI tool is available.

        Returns an error message string if the tool is missing, or ``None``
        if everything is fine.  Subclasses override as needed.
        """
        return None


class DryRunDispatcher(Dispatcher):
    """Records prompts without calling any LLM. Useful for testing."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, Task]] = []

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        self.dispatched.append((prompt, task))
        return DispatchResult(response=f"[dry-run] {task.name}: {prompt}", success=True)


def _llm_timeout(task: Task, default: float = 300) -> float:
    """Return the prompt/LLM timeout for a task."""
    if task.options.llm_timeout:
        return parse_duration_seconds(task.options.llm_timeout)
    return default


def _is_unknown_option_error(stderr: str) -> bool:
    """Return True when a CLI rejected a flag it does not know about."""
    lowered = (stderr or "").lower()
    return any(
        phrase in lowered
        for phrase in (
            "unknown option",
            "unrecognized option",
            "unknown argument",
            "no such option",
        )
    )


def _claude_dispatch_result(stdout: str, success: bool) -> DispatchResult:
    """Parse Claude CLI output, using the JSON envelope when present."""
    text = (stdout or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and "result" in data:
            usage = data.get("usage") or {}
            cost = data.get("total_cost_usd")
            return DispatchResult(
                response=str(data.get("result", "")),
                success=success and not data.get("is_error", False),
                tokens_in=_int_or_none(usage.get("input_tokens")),
                tokens_out=_int_or_none(usage.get("output_tokens")),
                cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            )
    return DispatchResult(response=stdout, success=success)


class ClaudeDispatcher(Dispatcher):
    """Dispatches prompts to the Claude CLI (`claude -p`)."""

    def __init__(self, model: str | None = None):
        self.default_model = model

    def validate_tool(self) -> str | None:
        if shutil.which("claude") is None:
            return "error: 'claude' CLI not found on PATH. Install it from https://docs.anthropic.com/en/docs/claude-code"
        return None

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        model = task.options.model or self.default_model
        cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt]
        if model:
            cmd.extend(["--model", model])
        # Name the subagent so it's identifiable as spawned by makethlm
        system_prompt = (
            f"[makethlm-{task.name}] You are a makethlm sub-agent executing task '{task.name}'."
        )
        cmd.extend(["--system-prompt", system_prompt])
        # Ask for the JSON envelope so usage and cost are reported. Older CLIs
        # that reject the flag fall back to plain text below.
        cmd.extend(["--output-format", "json"])

        try:
            result = run_subprocess(
                cmd,
                capture_output=True,
                text=True,
                timeout=_llm_timeout(task),
            )
            if result.returncode != 0 and _is_unknown_option_error(result.stderr):
                result = run_subprocess(
                    cmd[:-2],
                    capture_output=True,
                    text=True,
                    timeout=_llm_timeout(task),
                )
            return _claude_dispatch_result(result.stdout, result.returncode == 0)
        except FileNotFoundError:
            return DispatchResult(
                response="error: 'claude' CLI not found on PATH",
                success=False,
            )
        except subprocess.TimeoutExpired:
            return DispatchResult(
                response=f"error: claude CLI timed out after {_llm_timeout(task):.0f}s",
                success=False,
            )


class CodexDispatcher(Dispatcher):
    """Dispatches prompts to the Codex CLI (`codex exec`)."""

    def __init__(self, model: str | None = None, sandbox: str = "workspace-write"):
        self.default_model = model
        self.sandbox = sandbox

    def validate_tool(self) -> str | None:
        if shutil.which("codex") is None:
            return "error: 'codex' CLI not found on PATH. Install Codex CLI and run 'codex login'"
        return None

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        model = task.options.model or self.default_model
        cmd = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--sandbox",
            self.sandbox,
            "--color",
            "never",
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")

        try:
            result = run_subprocess(
                cmd,
                capture_output=True,
                text=True,
                timeout=_llm_timeout(task),
                input=prompt,
            )
            response = result.stdout
            if result.returncode != 0 and result.stderr and not response:
                response = result.stderr
            return DispatchResult(
                response=response,
                success=result.returncode == 0,
            )
        except FileNotFoundError:
            return DispatchResult(
                response="error: 'codex' CLI not found on PATH",
                success=False,
            )
        except subprocess.TimeoutExpired:
            return DispatchResult(
                response=f"error: codex CLI timed out after {_llm_timeout(task):.0f}s",
                success=False,
            )


class OpenAIDispatcher(Dispatcher):
    """Dispatches prompts to the OpenAI Chat Completions API."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.default_model = model
        self.api_key = api_key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    def _api_key(self) -> str | None:
        return self.api_key or os.environ.get("OPENAI_API_KEY")

    def validate_tool(self) -> str | None:
        if not self._api_key():
            return "error: OPENAI_API_KEY is not set for native OpenAI provider"
        return None

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        api_key = self._api_key()
        if not api_key:
            return DispatchResult(response="error: OPENAI_API_KEY is not set", success=False)

        model = task.options.model or self.default_model or "gpt-4o-mini"
        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if task.options.temperature is not None:
            payload["temperature"] = task.options.temperature
        if task.options.max_tokens is not None:
            payload["max_tokens"] = task.options.max_tokens

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_llm_timeout(task)) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage") or {}
            return DispatchResult(
                response=content,
                success=bool(content),
                tokens_in=_int_or_none(usage.get("prompt_tokens")),
                tokens_out=_int_or_none(usage.get("completion_tokens")),
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            return DispatchResult(
                response=f"error: OpenAI API returned {e.code}: {detail}", success=False
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            return DispatchResult(response=f"error: OpenAI API request failed: {e}", success=False)


class OllamaDispatcher(Dispatcher):
    """Dispatches prompts to a local Ollama HTTP server."""

    def __init__(self, model: str | None = None, base_url: str | None = None):
        self.default_model = model
        self.base_url = (base_url or "http://127.0.0.1:11434").rstrip("/")

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        model = task.options.model or self.default_model or "llama3"
        payload = {"model": model, "prompt": prompt, "stream": False}
        request = urllib.request.Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_llm_timeout(task)) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data.get("response", "")
            return DispatchResult(
                response=content,
                success=not data.get("error") and bool(content),
                tokens_in=_int_or_none(data.get("prompt_eval_count")),
                tokens_out=_int_or_none(data.get("eval_count")),
                # A local Ollama server has no per-token price.
                cost_usd=0.0,
            )
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            return DispatchResult(
                response=f"error: Ollama returned {e.code}: {detail}", success=False
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            return DispatchResult(response=f"error: Ollama request failed: {e}", success=False)


_NONINTERACTIVE_FLAGS: dict[str, str] = {
    "claude": "--dangerously-skip-permissions",
    "gemini": "--yolo",
}


_CODEX_SUBCOMMANDS = {
    "exec",
    "review",
    "login",
    "logout",
    "mcp",
    "plugin",
    "mcp-server",
    "app-server",
    "remote-control",
    "completion",
    "update",
    "sandbox",
    "debug",
    "apply",
    "resume",
    "fork",
    "cloud",
    "exec-server",
    "features",
    "help",
}


def _inject_codex_exec(template: str) -> str | None:
    """Rewrite simple Codex templates to use non-interactive `codex exec`."""
    m = re.match(r"^((?:\S*/)?codex)\b", template)
    if not m:
        return None

    tool = m.group(1)
    rest = template[m.end() :]
    stripped = rest.lstrip()
    first = stripped.split(None, 1)[0] if stripped else ""

    def _root_flags() -> str:
        return "" if "--ask-for-approval" in template else " --ask-for-approval never"

    def _exec_flags() -> str:
        flags = ""
        if "--sandbox" not in template:
            flags += " --sandbox workspace-write"
        if "--color" not in template:
            flags += " --color never"
        return flags

    if first == "exec":
        exec_match = re.match(r"^(\s+exec\b)(.*)$", rest)
        if exec_match:
            return tool + _root_flags() + exec_match.group(1) + _exec_flags() + exec_match.group(2)
        return template

    if first in _CODEX_SUBCOMMANDS:
        return template

    return tool + _root_flags() + " exec" + _exec_flags() + rest


def _inject_noninteractive_flags(template: str) -> str:
    """Inject non-interactive flags for known LLM CLI tools.

    Since we run with capture_output=True, interactive permission prompts
    are hidden from the user. This injects the appropriate skip-permissions
    flag for known tools so they run non-interactively.
    """
    codex_template = _inject_codex_exec(template)
    if codex_template is not None:
        return codex_template

    for tool, flag in _NONINTERACTIVE_FLAGS.items():
        # Match the tool name as the first command token (possibly path-qualified)
        pattern = rf"^((?:\S*/)?{re.escape(tool)})\b"
        m = re.match(pattern, template)
        if m and flag not in template:
            return template[: m.end()] + " " + flag + template[m.end() :]
    return template


class ShellDispatcher(Dispatcher):
    """Dispatches prompts to any LLM CLI via a configurable shell template.

    Example template: 'openai chat -m gpt-4 -p "{prompt}"'
    The {prompt} placeholder is replaced with the actual prompt.
    """

    def __init__(self, template: str):
        self.template = template

    def validate_tool(self) -> str | None:
        tool = _extract_tool_name(self.template)
        if tool is None:
            return None
        if shutil.which(tool) is None:
            return f"error: '{tool}' CLI not found on PATH"
        return None

    def dispatch(self, prompt: str, task: Task) -> DispatchResult:
        cmd = [
            part.replace("{prompt}", prompt)
            for part in shlex.split(_inject_noninteractive_flags(self.template))
        ]
        if not cmd:
            return DispatchResult(response="error: empty shell template", success=False)

        try:
            result = run_subprocess(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=_llm_timeout(task),
            )
            return DispatchResult(
                response=result.stdout,
                success=result.returncode == 0,
            )
        except subprocess.TimeoutExpired:
            return DispatchResult(
                response=f"error: command timed out after {_llm_timeout(task):.0f}s",
                success=False,
            )
