# Security

Promptfiles are executable automation. Review them like shell scripts before running them.

## Safe Mode

Use `--safe` to block risky execution unless each capability is explicitly allowed:

```bash
makethlm --safe deploy
makethlm --safe --allow-shell --allow-llm test
makethlm --safe --allow-ssh --allow-llm deploy
```

Safe mode disables backtick command substitution during parsing unless
`--allow-backticks` is passed. Runtime checks can block local shell steps, SSH
steps, docker blocks, and LLM prompt execution.

## Redaction

makethlm redacts likely secrets from command output, prompt output, artifacts,
and webhook payloads. Redaction checks environment and exported Promptfile
variables whose names contain terms such as `SECRET`, `TOKEN`, `PASSWORD`,
`PASS`, `API_KEY`, or `KEY`.

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
