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
