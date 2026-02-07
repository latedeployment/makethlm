# Secrets Injection

!!! warning "Planned Feature"
    This feature is not yet implemented.

## Syntax

```
{{#secret:NAME}}
```

Inside any step (shell, prompt, or echo), `{{#secret:NAME}}` is resolved
at runtime by querying the configured secrets backend. The `#` prefix
distinguishes secrets from regular variables — they are never stored in
the Promptfile's variable context and never appear in logs.

### Examples

```
task deploy:
    !curl -H "Authorization: Bearer {{#secret:DEPLOY_TOKEN}}" https://api.example.com/deploy

task notify:
    @echo "Deploying with key {{#secret:SLACK_WEBHOOK_ID}}"
    !curl -X POST {{#secret:SLACK_WEBHOOK_URL}} -d '{"text":"deployed"}'
```

### Nested paths

Some backends organize secrets hierarchically. Use `/` separators:

```
{{#secret:production/database/password}}
{{#secret:myapp/stripe/secret_key}}
```

## Backend Configuration

A new `set secrets` directive configures the backend. The CLI tool is
assumed to be already authenticated in the current session.

```
set secrets "infisical"
set secrets "1password"
set secrets "sops"
```

Or per-task override:

```
task deploy [secrets=infisical]:
    !deploy --token {{#secret:DEPLOY_TOKEN}}
```

### Backend: Infisical

Requires: `infisical` CLI, already logged in (`infisical login`).

Resolution: `infisical secrets get NAME --plain`

```
set secrets "infisical"
# Optional: specify project/environment
set secrets-project "my-project"
set secrets-environment "production"
```

Maps to:
```bash
infisical secrets get DEPLOY_TOKEN --plain \
    --projectId=my-project --env=production
```

### Backend: 1Password

Requires: `op` CLI, already signed in (`op signin`).

Resolution: `op read "op://vault/item/field"`

```
set secrets "1password"
set secrets-vault "DevOps"
```

Secret references map to 1Password paths:
```
{{#secret:DevOps/Deploy Token/credential}}
# -> op read "op://DevOps/Deploy Token/credential"
```

Or short form (uses configured vault):
```
{{#secret:Deploy Token/credential}}
# -> op read "op://DevOps/Deploy Token/credential"
```

### Backend: SOPS (with age)

Requires: `sops` CLI, `SOPS_AGE_KEY_FILE` or `SOPS_AGE_KEY` env var set.

Resolution: `sops decrypt --extract '["key"]' secrets.yaml`

```
set secrets "sops"
set secrets-file "secrets.yaml"     # encrypted file path
```

Maps to:
```bash
sops decrypt --extract '["DEPLOY_TOKEN"]' secrets.yaml
```

### Backend: env (fallback)

Simple environment variable lookup. Useful for CI/CD where secrets are
injected as env vars. This is the default if no backend is configured.

```
set secrets "env"
```

Maps to: `os.environ["DEPLOY_TOKEN"]`

## Security

- Secrets are never stored in the Promptfile's variable context
- Secrets never appear in logs (redacted as `***`)
- `--dry-run` shows `{{#secret:NAME}}` as `***` rather than resolving
- `--dump` / `--list` do not resolve secrets
- Secrets are never written to cache files or webhook payloads
