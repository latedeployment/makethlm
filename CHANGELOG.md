# Changelog

## Unreleased

### Added

#### Incremental builds

- Make-style file staleness with `sources` and `outputs`: a task is skipped when
  every output exists and is at least as new as every matched source.
- Source content digests folded into cache keys, so a `cache` duration expires as
  soon as an input file changes on disk.
- `--always-make`/`-B` to ignore staleness and cache skips for a run.
- `--watch` and `--watch-interval` to re-run a target when its `sources` or the
  Promptfile change.
- Cache keys that include task arguments, variables, providers, agents, options,
  artifacts, and referenced environment inputs.

#### Reliable workflows

- Failure postmortem tasks, typed artifact contracts, and retry/fallback LLM
  provider strategies.
- Output contract repair with `repair=N`, re-prompting the final prompt step when
  a response violates `produces`.
- Recorded LLM fixtures with `--fixtures DIR` and `--record-fixtures` for
  deterministic, offline, zero-spend runs that fail closed on a missing fixture.
- Per-provider `max-concurrency` caps and exponential backoff between retries of
  a rate-limited provider.
- Replayable, redacted local run bundles with `makethlm replay RUN_ID`.

#### Cost and budgets

- Token, cost, and call accounting per run with provider `price-in`/`price-out`
  declarations, a usage summary, and history columns.
- `--max-cost` and per-task `max-cost` budgets that stop a run once spend reaches
  the limit.

#### Multiple models

- Fan-out with `llm="a|b"`: one prompt to several providers concurrently, every
  answer kept as `{{task.provider.response}}`.
- `judge` to merge fan-out answers into a single response.
- `@llm <name>` and a prompt-line `|>` to chain one model's answer into the next.

#### Providers

- Native OpenAI and Ollama dispatchers via `llm openai`, `llm ollama`,
  `--openai`, and `--ollama`.
- opencode CLI provider via `llm opencode` and `--opencode`, treated as a
  local-execution provider under safe mode.
- MCP servers declared with `mcp <name> [command=... | url=...]` and attached per
  task with `mcp=`, translated per invocation for Claude (`--mcp-config`), Codex
  (`-c mcp_servers.*`), and opencode (`OPENCODE_CONFIG_CONTENT`), gated by
  `--allow-mcp` in safe mode.
- Codex token usage, reliable final-message capture, and native `--output-schema`
  enforcement of `produces` contracts.

#### Observability

- `--log-llm PATH` writes a redacted JSONL record of every LLM call, labeled by
  kind (prompt, repair, fan-out, judge, budget) for live debugging.
- Live elapsed-time indicator on a TTY while waiting for an LLM response.
- Transitive capability inspection with `--capabilities`.
- Machine-readable JSON output with `--json` for runs, plans, graphs, and history.

#### Inputs and syntax

- Make-style automatic variables inside a task: `{{makethlm_task}}` (`$@`),
  `{{makethlm_deps}}` (`$^`), `{{makethlm_dep}}` (`$<`), `{{makethlm_changed}}`
  (`$?`), plus `{{makethlm_sources}}` and `{{makethlm_outputs}}`.

- Git-aware inputs: `changed()`, `changed_files()`, `git_branch()`, `git_sha()`,
  and `--since REF` to scope tasks to a diff.
- Step-level shell output capture with `!cmd -> name`, `{{last.stdout}}`, and
  `!cmd |>` prompt piping.
- Module-scoped variables, functions, providers, agents, host groups, guidance,
  rollback hooks, and nested namespaces.
- Bare Just-style shell recipes, e.g. `build:` with plain shell command lines.
- Just-style `import`, optional `import?`, `[default]`, `[confirm("...")]`, and
  `[env(NAME, VALUE)]`.
- Just compatibility for shebang/script recipes, `script("COMMAND")`, modules,
  `&&` subsequent dependencies, multi-task invocation, shell arrays, and more
  built-ins.
- Hidden and all-caps Promptfile names (`.promptfile`, `.Promptfile`,
  `PROMPTFILE`, and their `.pf` forms).

#### Security

- Secrets injection with `{{#secret:NAME}}` and env, Infisical, 1Password, and
  SOPS backends.
- Secret backend tests, value-free secret audit logging, and a policy to block
  secrets in LLM prompts.
- Fail-closed sandbox selection, network-deny defaults, read-only workspace
  support, and sandbox working-directory propagation.

#### Execution

- Parallel CLI execution with `--parallel` and worker limits with `--jobs N`.
- Per-host Ansible inventory connection settings.

#### Tooling

- `makethlm fmt` with `--check` for canonical Promptfile formatting.
- Promptfile validation with `--check`.
- Shell completion generation with `makethlm completions bash|zsh|fish`.
- A clean `mypy` gate across the package, with strict checking on the focused
  modules, and contract checking extracted into `makethlm/contracts.py`.
- Dependency resolution extracted into `makethlm/graph.py` and the prompts
  makethlm composes itself (repair, judge, fan-out) into `makethlm/prompts.py`,
  both re-exported from `makethlm.runner` so existing imports keep working.
- Release preparation script for version bump, changelog, commit, tag, build, and
  publish flow.
- Local Ruff/dev dependency setup for linting and package smoke validation.
- Just compatibility tracking documentation.
- Real-world examples for Docker Compose, systemd, Kubernetes, Ansible inventory,
  Python package release, and CMake `compile_commands.json` analysis.

### Fixed

- `max-tokens` was rejected while every other multi-word task option accepted
  both hyphens and underscores. Both spellings now work.

- The README and docs landing-page examples were missing the trailing `:` on
  task headers with dependencies, so the first snippet a reader copies did not
  parse. Fixed across README, getting-started, index, docker, tasks, and syntax
  docs, with a test that parses every documented Promptfile snippet.

### Changed

- The README is now a landing page (1426 -> 245 lines) pointing at the published
  documentation site, instead of restating most of `docs/` a second time. It also
  covers `repair`, `judge`, and `mcp`, which it had never mentioned.

- The Claude CLI dispatcher requests the JSON envelope (`--output-format json`)
  so usage and cost are reported, retrying without it on CLIs that reject it.
- The Codex CLI dispatcher runs with `--json` and `--output-last-message`,
  reading the answer from the file rather than stdout, with the same fallback for
  older builds.
- `llm=` accepts a pipe-separated provider list; a single name behaves as before.
- Skipped tasks record an artifact with `success` set to `skipped`, so dependents
  and `when` conditions can observe them.

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
