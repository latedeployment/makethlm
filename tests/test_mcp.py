"""Tests for MCP server declarations and per-provider translation."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from makethlm.dispatcher import ClaudeDispatcher, CodexDispatcher, OpenCodeDispatcher
from makethlm.mcp import (
    MCPServer,
    claude_config,
    codex_overrides,
    opencode_config,
    split_command,
)
from makethlm.parser import ParseError, parse

LOCAL = MCPServer(name="files", command="npx", args=["-y", "server-fs", "/tmp"])
REMOTE = MCPServer(name="gh", url="https://example.com/mcp/")
WITH_ENV = MCPServer(name="db", command="db-mcp", env={"DB_URL": "postgres://x"})


class TestSplitCommand:
    def test_splits_program_and_args(self):
        assert split_command("npx -y server-fs /tmp") == ("npx", ["-y", "server-fs", "/tmp"])

    def test_respects_quoting(self):
        assert split_command('run "a b"') == ("run", ["a b"])

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="must not be empty"):
            split_command("   ")


class TestClaudeConfig:
    def test_local_server(self):
        config = json.loads(claude_config([LOCAL]))
        assert config["mcpServers"]["files"] == {
            "command": "npx",
            "args": ["-y", "server-fs", "/tmp"],
        }

    def test_remote_server(self):
        config = json.loads(claude_config([REMOTE]))
        assert config["mcpServers"]["gh"] == {"type": "http", "url": "https://example.com/mcp/"}

    def test_env_is_included(self):
        config = json.loads(claude_config([WITH_ENV]))
        assert config["mcpServers"]["db"]["env"] == {"DB_URL": "postgres://x"}


class TestCodexOverrides:
    def test_local_server(self):
        assert codex_overrides([LOCAL]) == [
            "-c",
            'mcp_servers.files.command="npx"',
            "-c",
            'mcp_servers.files.args=["-y", "server-fs", "/tmp"]',
        ]

    def test_remote_server(self):
        assert codex_overrides([REMOTE]) == [
            "-c",
            'mcp_servers.gh.url="https://example.com/mcp/"',
        ]

    def test_env_becomes_dotted_keys(self):
        assert 'mcp_servers.db.env.DB_URL="postgres://x"' in codex_overrides([WITH_ENV])

    def test_no_servers_is_empty(self):
        assert codex_overrides([]) == []


class TestOpenCodeConfig:
    def test_local_server(self):
        config = json.loads(opencode_config([LOCAL]))
        assert config["mcp"]["files"] == {
            "type": "local",
            "command": ["npx", "-y", "server-fs", "/tmp"],
            "enabled": True,
        }

    def test_remote_server(self):
        config = json.loads(opencode_config([REMOTE]))
        assert config["mcp"]["gh"]["type"] == "remote"
        assert config["mcp"]["gh"]["url"] == "https://example.com/mcp/"

    def test_env_uses_environment_key(self):
        config = json.loads(opencode_config([WITH_ENV]))
        assert config["mcp"]["db"]["environment"] == {"DB_URL": "postgres://x"}


class TestParsing:
    def test_local_declaration(self):
        pf = parse('mcp files [command="npx -y server-fs /tmp"]\n\ntask t [mcp=files]:\n    go\n')
        server = pf.mcp_servers["files"]
        assert server.command == "npx"
        assert server.args == ["-y", "server-fs", "/tmp"]

    def test_remote_declaration(self):
        pf = parse("mcp gh [url=https://example.com/mcp/]\n\ntask t [mcp=gh]:\n    go\n")
        assert pf.mcp_servers["gh"].is_remote

    def test_env_option(self):
        pf = parse(
            'mcp db [command=db-mcp, env(DB_URL, "postgres://x")]\n\ntask t [mcp=db]:\n    go\n'
        )
        assert pf.mcp_servers["db"].env == {"DB_URL": "postgres://x"}

    def test_servers_resolve_onto_the_task(self):
        pf = parse(
            'mcp a [url=https://a/]\nmcp b [url=https://b/]\n\ntask t [mcp="a|b"]:\n    go\n'
        )
        assert [s.name for s in pf.tasks["t"].mcp_servers] == ["a", "b"]

    def test_comma_separated_also_works(self):
        pf = parse(
            'mcp a [url=https://a/]\nmcp b [url=https://b/]\n\ntask t [mcp="a,b"]:\n    go\n'
        )
        assert len(pf.tasks["t"].mcp_servers) == 2

    def test_unknown_server_is_rejected(self):
        with pytest.raises(ParseError, match="unknown MCP server"):
            parse("task t [mcp=nope]:\n    go\n")

    def test_missing_name_is_rejected(self):
        with pytest.raises(ParseError, match="missing name"):
            parse("mcp [url=https://a/]\n\ntask t:\n    go\n")

    def test_command_and_url_conflict(self):
        with pytest.raises(ParseError, match="pick one"):
            parse("mcp a [command=x, url=https://a/]\n\ntask t:\n    go\n")

    def test_neither_command_nor_url(self):
        with pytest.raises(ParseError, match="needs either"):
            parse("mcp a [enabled=true]\n\ntask t:\n    go\n")

    def test_tasks_without_mcp_have_none(self):
        pf = parse("mcp a [url=https://a/]\n\ntask t:\n    go\n")
        assert pf.tasks["t"].mcp_servers == []


PF = """\
mcp files [command="npx -y server-fs /tmp"]

task review [mcp=files]:
    review it
"""


class TestDispatcherWiring:
    def _task(self):
        return parse(PF).tasks["review"]

    @patch("makethlm.dispatcher.run_subprocess")
    def test_claude_gets_mcp_config(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        ClaudeDispatcher().dispatch("go", self._task())
        cmd = mock_run.call_args.args[0]
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])
        assert "files" in config["mcpServers"]

    @patch("makethlm.dispatcher.run_subprocess")
    def test_codex_gets_config_overrides(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        CodexDispatcher().dispatch("go", self._task())
        cmd = mock_run.call_args.args[0]
        assert 'mcp_servers.files.command="npx"' in cmd

    @patch("makethlm.dispatcher.run_subprocess")
    def test_opencode_gets_inline_config(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        OpenCodeDispatcher().dispatch("go", self._task())
        config = json.loads(mock_run.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        assert config["mcp"]["files"]["type"] == "local"

    @patch("makethlm.dispatcher.run_subprocess")
    def test_no_flags_without_servers(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ok", stderr=""
        )
        task = parse("task review:\n    review it\n").tasks["review"]
        ClaudeDispatcher().dispatch("go", task)
        assert "--mcp-config" not in mock_run.call_args.args[0]
