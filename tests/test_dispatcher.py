"""Tests for dispatchers."""

from unittest.mock import patch

from makethlm.dispatcher import (
    ClaudeDispatcher,
    DryRunDispatcher,
    ShellDispatcher,
    _extract_tool_name,
)
from makethlm.models import Task, TaskStep, TaskOptions


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
