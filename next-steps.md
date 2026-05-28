# Next Steps

## DevOps/SRE Features Implemented

These items were added to make makethlm more useful in CI, deployment, and operations workflows.

### Execution Planning

Use `--plan` to preview the target, dependency order, variables, providers, host groups, task options, and resolved steps without running anything.

```bash
makethlm --plan deploy staging
```

### Dependency Graphs

Use `--graph` to print a task dependency graph. Mermaid is the default; DOT is available for Graphviz.

```bash
makethlm --graph deploy
makethlm --graph --graph-format dot deploy
```

### Parallel Task Execution

Use `--parallel` to run independent dependency tasks at the same graph depth
concurrently. Use `--jobs N` to cap concurrent task workers.

```bash
makethlm --parallel deploy
makethlm --jobs 4 deploy
```

If any task in a dependency level fails, later levels are skipped and the run
fails.

### Configurable Timeouts

Use task metadata to control shell/SSH and LLM timeouts separately.

```make
task deploy [timeout=45s, llm-timeout=5m]:
    !systemctl restart myapp
    verify the service is healthy
```

Supported duration units are `s`, `m`, `h`, and `d`. Bare numbers are treated as seconds.

### Stronger SSH Options

Host groups can define SSH identity files and host-key policy:

```make
hosts web [user=deploy, identity-file=~/.ssh/deploy, strict-host-key-checking=accept-new]:
    web1.example.com
    web2.example.com
```

Tasks can override those settings:

```make
task deploy [on=web, ssh-key=~/.ssh/prod, ssh-strict-host-key-checking=yes]:
    !uptime
```

### Parallel SSH Host Execution

Use `ssh-parallel` to run each shell step across all hosts in a target group concurrently.

```make
task restart [on=web, ssh-parallel, timeout=30s]:
    !systemctl restart myapp
```

### Rollback Hooks

Use `rollback=<task>` to run a recovery task when a task fails.

```make
task rollback-deploy:
    !systemctl restart previous-myapp

task deploy [rollback=rollback-deploy]:
    !systemctl restart myapp
```

The original run remains failed even if rollback succeeds.

## Self-Hosted Features Implemented

### Local Run History

Runs are recorded in SQLite under `$MAKETHLM_HISTORY_DB` or
`~/.local/share/makethlm/history.sqlite`.

```bash
makethlm history
makethlm --history 50
makethlm deploy --no-history
```

### Local Web UI/API

Start a small stdlib-only HTTP server for local/self-hosted operation:

```bash
makethlm --serve 127.0.0.1:8765
```

Available endpoints:

- `GET /api/tasks`
- `GET /api/history`
- `POST /api/run?task=name`

### Webhook Presets

Webhook presets format payloads for common self-hosted/chat systems:

```make
task deploy [webhook=ntfy:https://ntfy.sh/my-topic]:
    !deploy

task notify [webhook=gotify:https://gotify.example/message?token=...]:
    !echo done
```

Supported presets are `ntfy:`, `gotify:`, `discord:`, and `slack:`.

## Python Developer Features Implemented

### Public API Exports

Core objects are importable from `makethlm`:

```python
from makethlm import parse, Runner, DryRunDispatcher
```

### Ruff Configuration

`pyproject.toml` now includes baseline Ruff settings for future lint adoption.

### Golden Promptfile Tests

`tests/golden/devops.pf` captures a realistic Promptfile fixture for parser
regression tests.

## Security Improvements Implemented

### Safe Mode

`--safe` blocks risky capabilities unless explicitly allowed:

```bash
makethlm --safe --allow-shell --allow-llm test
makethlm --safe --allow-ssh deploy
```

Backtick command substitution is disabled during parsing unless
`--allow-backticks` is passed.

### Secret Redaction

Likely secrets from environment variables and exported Promptfile variables are
redacted from command output, prompt output, artifacts, and webhook payloads.

### Threat Model Documentation

`docs/security.md` documents executable Promptfile risks and safer review
workflows.

### Secrets Injection

`{{#secret:NAME}}` placeholders are resolved at runtime with configurable
backends:

- `env`
- `infisical`
- `1password`
- `sops`

Secrets are masked in `--plan` and `--dry-run`, and tasks using secrets bypass
the result cache to avoid stale secret-dependent outputs.

## Language/Workflow Examples Added

- `examples/cmake-project/Promptfile`: C/C++ CMake configure/build/test flow.
- `examples/compiler-diagnostics/Promptfile`: compiler output artifact analysis.
- `examples/python-ci/Promptfile`: Python lint, compile, test, and security review.

## Remaining Feature Gaps

These are the highest-value items still missing or only partially implemented.

### Release and Packaging Cleanup

- Add CI for tests, lint, build, and package install.
- Remove or ignore stale generated artifacts.
- Automate changelog, version bump, tag, build, and publish steps.
- Update the Git remote to `git@github.com:latedeployment/makethlm.git`.

### Machine-Readable Output

- Add `--json` for run results.
- Support JSON plan output.
- Support JSON history output.
- Include task name, status, elapsed time, prompt, response, exit code, and steps.

### Lint and Type Pipeline

- Enforce `uv run ruff check .`.
- Consider `ruff format`.
- Add `mypy` or `pyright` after the larger modules are split.

### Better Web UI

`--serve` is currently a small stdlib UI/API.

- Add task detail pages.
- Add argument forms.
- Add run logs and history detail pages.
- Add live output streaming.
- Add safe-mode controls.

### Secrets Hardening

- Mock and test `infisical`, `op`, and `sops` subprocess calls.
- Add secret resolution audit logs without values.
- Add an optional policy to deny secrets in LLM prompts.

### Provider Polish

- Add native OpenAI and Ollama dispatchers instead of relying only on shell templates.
- Improve provider-specific validation and error messages.
- Document provider behavior consistently.

### Promptfile Validation

Add `makethlm check` to validate:

- syntax
- dependency references
- missing rollback targets
- provider names
- host groups
- agent files
- backend tools
- unsafe feature use

### Shell Completions

- Add `makethlm completions bash`.
- Add `makethlm completions zsh`.
- Add `makethlm completions fish`.
- Include local task-name completion where possible.

### Module Cleanup

Split the largest modules into focused pieces:

- secrets and interpolation
- parser helpers
- task execution
- SSH execution
- webhooks
- history

### Sandbox Hardening

- Add deeper tests for Docker, systemd, and bwrap sandbox modes.
- Add read-only workspace mode.
- Add network-deny defaults.
- Add explicit mount controls.
- Document sandbox threat boundaries.

### More Real-World Examples

- Docker Compose deployment
- systemd service deployment
- Kubernetes/kubectl workflows
- Ansible inventory import
- GitHub Actions usage
- Python package release
- CMake with `compile_commands.json` analysis

## Implementation Queue

Work through these tasks one by one. Each task should end with tests, docs, and
a focused commit before starting the next item.

1. Add `--json` run output.
   Emit machine-readable run results with target, task status, duration, step
   kinds, prompts, responses, success flags, and exit codes.

2. Add JSON support for `--plan`, `--history`, and `--graph`.
   Reuse the same schemas where possible and keep text output as the default.

3. Add `makethlm check`.
   Validate syntax, dependency references, rollback targets, provider names,
   host groups, agent files, backend tools, and unsafe feature use.

4. Add CI.
   Run `uv run pytest tests/ -q --no-docker`, `uv run ruff check .`, package
   build, and a wheel install smoke test.

5. Enforce Ruff locally.
   Make the existing Ruff config actionable, fix reported issues, and document
   the command in README and contributor docs.

6. Clean release packaging.
   Remove stale generated artifacts from git, update `.gitignore`, refresh the
   remote URL, and add repeatable build/release commands.

7. Add shell completions.
   Implement `makethlm completions bash|zsh|fish`, including task names when a
   Promptfile is available.

8. Harden secrets backends.
    Add subprocess-mocked tests for `infisical`, `op`, and `sops`, add value-free
    audit messages, and add an option to forbid secret injection into LLM prompts.

9. Add native OpenAI dispatcher.
    Implement direct subprocess/API behavior according to the package direction,
    provider validation, timeout handling, tests, and docs.

10. Add native Ollama dispatcher.
    Support local Ollama execution with model selection, timeout handling,
    provider validation, tests, and docs.

11. Improve `--serve`.
    Add task detail views, argument forms, run history detail pages, and safer
    execution controls.

12. Add live output to `--serve`.
    Stream task output to the UI/API without waiting for the run to finish.

13. Split secrets and interpolation out of `models.py`.
    Move secret resolution and interpolation helpers into focused modules while
    preserving the public API and existing tests.

14. Split runner subsystems.
    Extract SSH execution, webhooks, history recording, and sandbox wrapping into
    focused modules with targeted tests.

15. Harden sandbox behavior.
    Add tests for Docker, systemd, and bwrap modes, read-only workspace support,
    network-deny defaults, explicit mounts, and threat-boundary docs.

16. Add Docker Compose and systemd examples.
    Provide realistic Promptfiles and docs for common self-hosted deployments.

17. Add Kubernetes and Ansible examples.
    Cover `kubectl` workflows, inventory import, remote deploys, and rollback
    patterns.

18. Add release workflow examples.
    Include Python package release, GitHub Actions usage, and CMake
    `compile_commands.json` analysis examples.
