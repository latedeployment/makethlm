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
- Step-level shell output capture with `!cmd -> name`, `{{last.stdout}}`, and `!cmd |>` prompt piping.
- Local Ruff/dev dependency setup for linting and package smoke validation.
- Just-style `import`, optional `import?`, `[default]`, `[confirm("...")]`, and `[env(NAME, VALUE)]`.
- Shell completion generation with `makethlm completions bash|zsh|fish`.
- Native OpenAI and Ollama dispatchers via `llm openai`, `llm ollama`, `--openai`, and `--ollama`.
- Release preparation script for version bump, changelog, commit, tag, build, and publish flow.
- Secret backend tests, value-free secret audit logging, and a policy to block secrets in LLM prompts.
- Just compatibility for shebang/script recipes, `script("COMMAND")`, modules, `&&` subsequent dependencies, multi-task invocation, shell arrays, and more built-ins.
- Real-world examples for Docker Compose, systemd, Kubernetes, Ansible inventory, Python package release, and CMake `compile_commands.json` analysis.
- Fail-closed sandbox selection, network-deny defaults, read-only workspace support, and sandbox working-directory propagation.
- Failure postmortem tasks, typed artifact contracts, and retry/fallback LLM provider strategies.
- Transitive capability inspection with `--capabilities`.
- Replayable, redacted local run bundles with `makethlm replay RUN_ID`.
- Cache keys that include task arguments, variables, providers, agents, options, artifacts, and referenced environment inputs.
- Module-scoped variables, functions, providers, agents, host groups, guidance, rollback hooks, and nested namespaces.
- Per-host Ansible inventory connection settings.
- Make-style file staleness with `sources` and `outputs`, source content digests
  in cache keys, and `--always-make`/`-B` to force a run.
- Output contract repair with `repair=N`, re-prompting the final prompt step when
  a response violates `produces`.
- Recorded LLM fixtures with `--fixtures DIR` and `--record-fixtures` for
  deterministic, offline, zero-spend runs that fail closed on a missing fixture.
- Token, cost, and call accounting per run with provider `price-in`/`price-out`
  declarations, a usage summary, history columns, and `--max-cost`/`max-cost`
  budgets that stop a run once spend reaches the limit.
- `--watch` and `--watch-interval` to re-run a target when its `sources` or the
  Promptfile change.
- Git-aware inputs: `changed()`, `changed_files()`, `git_branch()`, `git_sha()`,
  and `--since REF` to scope tasks to a diff.
- Per-provider `max-concurrency` caps and exponential backoff between retries of
  a rate-limited provider.
- Live elapsed-time indicator on a TTY while waiting for an LLM response.
- `makethlm fmt` with `--check` for canonical Promptfile formatting.
- A clean `mypy` gate across the package, with strict checking on the focused
  modules, and contract checking extracted into `makethlm/contracts.py`.
- Hidden and all-caps Promptfile names (`.promptfile`, `.Promptfile`, `PROMPTFILE`,
  and their `.pf` forms).
- Multi-model tasks: `llm="a|b"` fans one prompt out to several providers
  concurrently and keeps every answer as `{{task.provider.response}}`, `judge`
  merges them into one response, and `@llm <name>` plus a prompt-line `|>` chains
  one model's answer into the next.

## 0.1.0 - 2026-05-24

### Added

- First-class Codex CLI provider support via `llm codex` and `--codex`.
- Execution planning with `--plan`.
- Dependency graph output with `--graph` and `--graph-format mermaid|dot`.
- Task timeouts with `timeout` for shell/SSH and `llm-timeout` for LLM calls.
- SSH identity files, host key policies, and `ssh-parallel` host execution.
- Rollback hooks with `rollback=<task>`.
- Local SQLite run history with `history`, `--history`, and `--no-history`.
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
