# Changelog

## Unreleased

### Added

- Secrets injection with `{{#secret:NAME}}` and env, Infisical, 1Password, and SOPS backends.
- Parallel CLI execution with `--parallel`.
- Parallel worker limits with `--jobs N`.
- Machine-readable JSON output with `--json` for runs, plans, graphs, and history.
- Bare Just-style shell recipes, e.g. `build:` with plain shell command lines.
- Just compatibility tracking documentation.
- Promptfile validation with `--check`.

## 0.1.0 - 2026-05-24

### Added

- First-class Codex CLI provider support via `llm codex` and `--codex`.
- Execution planning with `--plan`.
- Dependency graph output with `--graph` and `--graph-format mermaid|dot`.
- Task timeouts with `timeout` for shell/SSH and `llm-timeout` for LLM calls.
- SSH identity files, host key policies, and `ssh-parallel` host execution.
- Rollback hooks with `rollback=<task>`.
- Local SQLite run history with `history`, `--history`, and `--no-history`.
- Local self-hosted UI/API server with `--serve`.
- Safe mode with explicit `--allow-*` permissions.
- Parse-time backtick blocking in safe mode.
- Secret redaction for likely secret environment and exported variables.
- Webhook presets for ntfy, Gotify, Discord, and Slack.
- Public Python API exports from `makethlm`.
- Ruff configuration and golden Promptfile tests.
- Workflow examples for CMake, compiler diagnostics, and Python CI.
- Repository contributor guide in `AGENTS.md`.
- Pre-commit security review task and contributor workflow.

### Changed

- Shell-template Codex commands are rewritten to non-interactive `codex exec`.
- Documentation now covers Codex, security, DevOps features, self-hosted usage, and new workflow examples.

## 0.0.1 - 2026-05-24

### Added

- Initial makethlm package, Promptfile parser, CLI runner, dispatcher support, docs, examples, and tests.
