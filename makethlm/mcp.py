"""MCP server declarations and per-provider translation.

A Promptfile declares MCP servers once; each CLI provider is configured for a
single invocation without touching the user's global configuration:

- Claude CLI takes an inline JSON document via ``--mcp-config``.
- Codex takes dotted ``-c`` config overrides.
- opencode reads inline JSON from ``OPENCODE_CONFIG_CONTENT``.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field


@dataclass
class MCPServer:
    """One MCP server: either a local stdio command or a remote URL."""

    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None

    @property
    def is_remote(self) -> bool:
        return bool(self.url)


def split_command(value: str) -> tuple[str, list[str]]:
    """Split a shell-style command string into its program and arguments."""
    parts = shlex.split(value)
    if not parts:
        raise ValueError("mcp command must not be empty")
    return parts[0], parts[1:]


def claude_config(servers: list[MCPServer]) -> str:
    """Return the inline JSON document Claude's ``--mcp-config`` expects."""
    entries: dict[str, dict[str, object]] = {}
    for server in servers:
        if server.is_remote:
            entries[server.name] = {"type": "http", "url": server.url}
        else:
            entry: dict[str, object] = {"command": server.command, "args": list(server.args)}
            if server.env:
                entry["env"] = dict(server.env)
            entries[server.name] = entry
    return json.dumps({"mcpServers": entries}, sort_keys=True)


def codex_overrides(servers: list[MCPServer]) -> list[str]:
    """Return ``-c key=value`` argv pairs configuring Codex for one invocation.

    Values are TOML fragments, which is what ``codex -c`` parses.
    """
    argv: list[str] = []
    for server in servers:
        base = f"mcp_servers.{server.name}"
        if server.is_remote:
            argv.extend(["-c", f"{base}.url={json.dumps(server.url)}"])
            continue
        argv.extend(["-c", f"{base}.command={json.dumps(server.command)}"])
        if server.args:
            argv.extend(["-c", f"{base}.args={json.dumps(server.args)}"])
        for key, value in sorted(server.env.items()):
            argv.extend(["-c", f"{base}.env.{key}={json.dumps(value)}"])
    return argv


def opencode_config(servers: list[MCPServer]) -> str:
    """Return the inline JSON for ``OPENCODE_CONFIG_CONTENT``."""
    entries: dict[str, dict[str, object]] = {}
    for server in servers:
        if server.is_remote:
            entries[server.name] = {"type": "remote", "url": server.url, "enabled": True}
        else:
            entry: dict[str, object] = {
                "type": "local",
                "command": [server.command, *server.args],
                "enabled": True,
            }
            if server.env:
                entry["environment"] = dict(server.env)
            entries[server.name] = entry
    return json.dumps({"mcp": entries}, sort_keys=True)
