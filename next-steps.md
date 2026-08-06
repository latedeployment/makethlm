# Next Steps

This queue reflects the current direction for makethlm: stay close to
`make`/`just`, keep the CLI-first workflow, and avoid adding a web UI/API or
GitHub Actions.

## Highest-Value Work

### 1. Split Large Modules

Break up the modules that currently carry too many responsibilities:

- Move secrets and interpolation out of `models.py`.
- Move parser helpers into focused parser modules.
- Move SSH execution out of `runner.py`.
- Move Docker/sandbox execution out of `runner.py`.
- Move webhook and history integration out of `runner.py`.

Each split should preserve the public API and land with focused tests.

### 2. Harden Sandboxes

Improve Docker, systemd, and bwrap behavior:

- Add deeper sandbox tests.
- Add read-only workspace mode.
- Add network-deny defaults.
- Add explicit mount controls.
- Document sandbox threat boundaries.

### 3. Improve Just Compatibility

Close the main compatibility gaps:

- Nested module parity and richer module listing.
- Unqualified module imports where appropriate.
- Exact `[metadata]` behavior.
- Fuller `[env]` semantics.
- More built-in functions and path edge cases.

### 4. Improve CLI Listing

Make `--list` more useful without leaving the terminal:

- Show aliases and module tasks clearly.
- Surface task attributes and docs consistently.
- Add JSON parity where useful.

### 5. Add Type and Format Pipeline

After module splitting reduces complexity:

- Add `mypy` or `pyright`.
- Keep type checks scoped enough to be maintainable.

## Security and Ops

### Secrets Hardening

- Add backend-specific failure-mode tests for `infisical`, `op`, and `sops`.
- Keep secret values out of logs, cache, history, and prompt output.
- Keep testing the no-secrets-in-prompts policy.

### Provider Polish

- Improve provider-specific validation and error messages.
- Add more native OpenAI and Ollama compatibility tests.
- Keep CLI-template dispatch argv-based and non-shell.

### Release Packaging

- Add release signing or provenance if publishing requires it.
- Keep generated artifacts ignored and out of commits.

## Implementation Queue

Completed in this pass:

- Extract secrets from `models.py` into `makethlm/secrets.py`.
- Extract interpolation helpers from `models.py` into `makethlm/interpolation.py`.
- Extract SSH command construction and validation from `runner.py` into
  `makethlm/ssh.py`.
- Extract SSH execution result handling into `makethlm/ssh.py`.
- Extract Docker path/build helpers into `makethlm/docker.py`.
- Extract sandbox command construction into `makethlm/sandbox.py`.
- Extract webhook request construction and delivery into `makethlm/webhooks.py`.
- Improve `--list` output for modules, aliases, attributes, and JSON output.
- Preserve module-scoped aliases as `module::alias -> module::task`.
- Harden sandbox command construction with Docker `--net none` by default and
  `sandbox-read-only` support.
- Add GitLab CI, Forgejo Actions, rollback-pattern, and Kubernetes dry-run
  workflow examples without adding repo-level hosted CI config.
- Add `ruff format --check` to local release and publish validation.

Next tasks, in order:

1. Continue expanding Just module compatibility.
2. Add deeper sandbox tests and threat-boundary docs.
3. Continue parser module splitting.
