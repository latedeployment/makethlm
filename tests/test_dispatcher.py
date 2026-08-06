"""Tests for dispatchers."""

import json
import subprocess
from unittest.mock import patch

from makethlm.dispatcher import (
    ClaudeDispatcher,
    CodexDispatcher,
    DryRunDispatcher,
    OllamaDispatcher,
    OpenAIDispatcher,
    ShellDispatcher,
    _extract_tool_name,
    _inject_noninteractive_flags,
)
from makethlm.models import Task, TaskOptions, TaskStep


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TestDryRunDispatcher:
    def test_returns_success(self):
        d = DryRunDispatcher()
        task = Task(name="build", steps=[TaskStep(kind="prompt", content="build it")])
        result = d.dispatch("build it", task)
        assert result.success
        assert "build" in result.response

    def test_records_dispatches(self):
        d = DryRunDispatcher()
        t1 = Task(name="a", steps=[TaskStep(kind="prompt", content="do a")])
        t2 = Task(name="b", steps=[TaskStep(kind="prompt", content="do b")])
        d.dispatch("do a", t1)
        d.dispatch("do b", t2)
        assert len(d.dispatched) == 2
        assert d.dispatched[0] == ("do a", t1)
        assert d.dispatched[1] == ("do b", t2)

    def test_response_contains_task_name(self):
        d = DryRunDispatcher()
        task = Task(name="deploy", steps=[TaskStep(kind="prompt", content="deploy now")])
        result = d.dispatch("deploy now", task)
        assert "deploy" in result.response


class TestExtractToolName:
    def test_simple_command(self):
        assert _extract_tool_name('codex "{prompt}"') == "codex"

    def test_path_qualified(self):
        assert _extract_tool_name('/usr/local/bin/codex "{prompt}"') == "codex"

    def test_empty_string(self):
        assert _extract_tool_name("") is None

    def test_whitespace_only(self):
        assert _extract_tool_name("   ") is None

    def test_single_token(self):
        assert _extract_tool_name("gemini") == "gemini"


class TestValidateTool:
    def test_dry_run_always_valid(self):
        d = DryRunDispatcher()
        assert d.validate_tool() is None

    @patch("makethlm.dispatcher.shutil.which", return_value="/usr/bin/claude")
    def test_claude_found(self, mock_which):
        d = ClaudeDispatcher()
        assert d.validate_tool() is None
        mock_which.assert_called_with("claude")

    @patch("makethlm.dispatcher.shutil.which", return_value=None)
    def test_claude_not_found(self, mock_which):
        d = ClaudeDispatcher()
        err = d.validate_tool()
        assert err is not None
        assert "claude" in err

    @patch("makethlm.dispatcher.shutil.which", return_value="/usr/bin/codex")
    def test_codex_found(self, mock_which):
        d = CodexDispatcher()
        assert d.validate_tool() is None
        mock_which.assert_called_with("codex")

    @patch("makethlm.dispatcher.shutil.which", return_value=None)
    def test_codex_not_found(self, mock_which):
        d = CodexDispatcher()
        err = d.validate_tool()
        assert err is not None
        assert "codex" in err

    @patch("makethlm.dispatcher.shutil.which", return_value="/usr/bin/codex")
    def test_shell_tool_found(self, mock_which):
        d = ShellDispatcher('codex "{prompt}"')
        assert d.validate_tool() is None
        mock_which.assert_called_with("codex")

    @patch("makethlm.dispatcher.shutil.which", return_value=None)
    def test_shell_tool_not_found(self, mock_which):
        d = ShellDispatcher('codex "{prompt}"')
        err = d.validate_tool()
        assert err is not None
        assert "codex" in err

    def test_shell_empty_template(self):
        d = ShellDispatcher("")
        assert d.validate_tool() is None


class TestNoninteractiveFlags:
    def test_codex_template_uses_exec(self):
        cmd = _inject_noninteractive_flags('codex "{prompt}"')
        assert cmd == (
            'codex --ask-for-approval never exec --sandbox workspace-write --color never "{prompt}"'
        )

    def test_codex_exec_template_gets_safe_defaults(self):
        cmd = _inject_noninteractive_flags('codex exec "{prompt}"')
        assert cmd == (
            'codex --ask-for-approval never exec --sandbox workspace-write --color never "{prompt}"'
        )

    def test_codex_exec_template_preserves_explicit_sandbox(self):
        cmd = _inject_noninteractive_flags('codex exec --sandbox read-only "{prompt}"')
        assert cmd == (
            'codex --ask-for-approval never exec --color never --sandbox read-only "{prompt}"'
        )


class TestShellDispatcher:
    @patch("makethlm.dispatcher.run_subprocess")
    def test_dispatch_uses_argv_not_shell(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="done",
            stderr="",
        )
        task = Task(name="review", steps=[TaskStep(kind="prompt", content="review it")])
        d = ShellDispatcher('llm --prompt "{prompt}"')

        result = d.dispatch('hello $(touch /tmp/pwn) "quoted"', task)

        assert result.success
        assert mock_run.call_args.args[0] == [
            "llm",
            "--prompt",
            'hello $(touch /tmp/pwn) "quoted"',
        ]
        assert mock_run.call_args.kwargs["shell"] is False


class TestCodexDispatcher:
    @patch("makethlm.dispatcher.run_subprocess")
    def test_dispatch_uses_codex_exec_with_prompt_on_stdin(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="done",
            stderr="",
        )
        task = Task(name="review", steps=[TaskStep(kind="prompt", content="review it")])
        d = CodexDispatcher(model="gpt-5-codex")

        result = d.dispatch("review it", task)

        assert result.success
        assert result.response == "done"
        cmd = mock_run.call_args.args[0]
        assert cmd[:4] == ["codex", "--ask-for-approval", "never", "exec"]
        assert "--sandbox" in cmd
        assert "workspace-write" in cmd
        assert "--color" in cmd
        assert "never" in cmd
        assert ["--model", "gpt-5-codex"] == cmd[cmd.index("--model") : cmd.index("--model") + 2]
        assert cmd[-1] == "-"
        assert mock_run.call_args.kwargs["input"] == "review it"

    @patch("makethlm.dispatcher.run_subprocess")
    def test_task_model_overrides_default_model(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="done",
            stderr="",
        )
        task = Task(
            name="review",
            steps=[TaskStep(kind="prompt", content="review it")],
            options=TaskOptions(model="gpt-5.1-codex"),
        )
        d = CodexDispatcher(model="gpt-5-codex")

        d.dispatch("review it", task)

        cmd = mock_run.call_args.args[0]
        assert ["--model", "gpt-5.1-codex"] == cmd[cmd.index("--model") : cmd.index("--model") + 2]

    @patch("makethlm.dispatcher.run_subprocess")
    def test_llm_timeout_option_is_used(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="done",
            stderr="",
        )
        task = Task(
            name="review",
            steps=[TaskStep(kind="prompt", content="review it")],
            options=TaskOptions(llm_timeout="2m"),
        )
        d = CodexDispatcher()

        d.dispatch("review it", task)

        assert mock_run.call_args.kwargs["timeout"] == 120


class TestOpenAIDispatcher:
    def test_validate_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        err = OpenAIDispatcher().validate_tool()

        assert err is not None
        assert "OPENAI_API_KEY" in err

    @patch("makethlm.dispatcher.urllib.request.urlopen")
    def test_dispatch_posts_chat_completion_request(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse(
            {
                "choices": [{"message": {"content": "review complete"}}],
            }
        )
        task = Task(name="review", steps=[TaskStep(kind="prompt", content="review it")])
        dispatcher = OpenAIDispatcher(
            model="gpt-test",
            api_key="test-key",
            base_url="https://example.test/v1",
        )

        result = dispatcher.dispatch("review it", task)

        assert result.success
        assert result.response == "review complete"
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        assert request.full_url == "https://example.test/v1/chat/completions"
        assert payload["model"] == "gpt-test"
        assert payload["messages"][0]["content"] == "review it"
        assert request.headers["Authorization"] == "Bearer test-key"


class TestOllamaDispatcher:
    @patch("makethlm.dispatcher.urllib.request.urlopen")
    def test_dispatch_posts_generate_request(self, mock_urlopen):
        mock_urlopen.return_value = FakeHTTPResponse({"response": "done"})
        task = Task(name="review", steps=[TaskStep(kind="prompt", content="review it")])
        dispatcher = OllamaDispatcher(model="llama-test", base_url="http://ollama.test")

        result = dispatcher.dispatch("review it", task)

        assert result.success
        assert result.response == "done"
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        assert request.full_url == "http://ollama.test/api/generate"
        assert payload == {
            "model": "llama-test",
            "prompt": "review it",
            "stream": False,
        }
