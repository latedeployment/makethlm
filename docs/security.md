# Security

Promptfiles are executable automation. Review them like shell scripts before running them.

## Safe Mode

Use `--safe` to block risky execution unless each capability is explicitly allowed:

```bash
makethlm --safe deploy
makethlm --safe --allow-shell --allow-llm test
makethlm --safe --allow-ssh --allow-llm deploy
makethlm --safe --allow-llm --allow-secrets review
makethlm --capabilities deploy
```

Safe mode disables backtick command substitution during parsing unless
`--allow-backticks` is passed. Inspection modes such as `--check`,
`--capabilities`, `--plan`, `--graph`, `--list`, and `--dry-run` also disable
backticks by default, so inspecting an unfamiliar Promptfile does not run its
parse-time commands. Runtime checks can block local shell steps, SSH
steps, docker blocks, LLM prompt execution, external secret backends, runtime
shell conditions, attached MCP servers, and webhooks. Failure postmortem and rollback task closures
are checked too. Webhook delivery requires `--allow-webhook`, attaching MCP servers requires
`--allow-mcp`, and secret
placeholders or sensitive environment/template inputs require
`--allow-secrets`. Every explicit environment read such as `${NAME}` or
`env_var("NAME")` is permissioned; authorization does not depend on guessing
whether its name looks sensitive.

Claude CLI, Codex CLI, opencode, and custom shell-template LLM providers can
execute local tools or write files. Safe mode therefore requires both
`--allow-llm` and `--allow-shell` for prompt tasks that can reach one of those
providers.
Native OpenAI and Ollama HTTP providers require `--allow-llm` but do not add
the local shell capability.

`--capabilities` prints the exact transitive task closure, why each capability
is needed, and the safe-mode flag that grants it. Add `--json` for policy tools.

## Redaction

makethlm redacts likely secrets from plans, dumps, command output, prompt
output, artifacts, history replay bundles, and webhook payloads. Redaction
checks the environment and Promptfile
variables whose names contain terms such as `SECRET`, `TOKEN`, `PASSWORD`,
`PASS`, `API_KEY`, `KEY`, `AUTH`, `COOKIE`, `SESSION`, or `DATABASE_URL`.

Sandbox backends fail closed when an unknown backend is requested. Docker,
systemd, and bwrap sandbox networking is denied by default unless
`sandbox-net=host` explicitly grants it. Unknown network modes are rejected.
A sandbox is a containment layer, not a
substitute for reviewing the command and mount list.

## Webhook Presets

Webhook URLs can use presets:

```make
task deploy [webhook=ntfy:https://ntfy.sh/my-topic]:
    !deploy

task notify [webhook=discord:https://discord.com/api/webhooks/...]:
    !echo done
```

Supported presets are `ntfy:`, `gotify:`, `discord:`, and `slack:`. Plain URLs
still receive the default JSON payload.

## Threat Model

The main risks are local command execution, parse-time backtick commands, remote
SSH execution, LLM tool execution, generated Dockerfiles, and accidental secret
exposure. Prefer `--plan`, `--dry-run`, and `--safe` when evaluating Promptfiles
from another person or repository.
