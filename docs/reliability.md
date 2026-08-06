# Reliable Workflows

makethlm can make failure handling and task boundaries explicit without
requiring a separate orchestration service.

## Failure Postmortems

`postmortem` runs a diagnostic task after a failure and before `rollback`.
The failed task's redacted artifact fields are available to the diagnostic
task:

```make
task diagnose:
    explain why deploy exited with {{deploy.exit_code}}:
    {{deploy.stdout}}

task restore:
    !./scripts/restore-release

task deploy [postmortem=diagnose, rollback=restore]:
    !./scripts/deploy-release
```

The postmortem and rollback results are included in normal output, JSON output,
history, capability inspection, and safe-mode validation.

## Artifact Contracts

Use `requires` to validate upstream artifact fields before a task starts:

```make
task inventory:
    !./scripts/inventory --json

task deploy [requires="inventory.stdout:object"]: inventory:
    !./scripts/deploy
```

Use `produces` to validate a task's aggregate output:

```make
task inspect [produces=object]:
    !./scripts/inspect --json
```

Supported types are `text`, `nonempty`, `json`, `object`, `array`, `integer`,
`number`, and `boolean`. Multiple requirements use `|`:

```make
task publish [requires="build.stdout:nonempty|metadata.stdout:object"]: build metadata:
    !./scripts/publish
```

Contracts fail closed with exit code 2 and appear as `contract` steps in JSON
and replay bundles.

## Provider Retry and Fallback

`retries` repeats each provider before advancing through `fallback-llm`:

```make
llm cloud [template=cloud-llm {prompt}]
llm local [model=llama3]

task review [llm=cloud, retries=1, fallback-llm=local]:
    review the release diff
```

The successful provider and total attempt number are recorded on the prompt
step. Retries are limited to 10 per provider, and fallback chains accept up to
four distinct providers.

## Reproducible Caching

Enable caching by setting a duration on the task:

```make
task inspect [cache=1h, produces=object]:
    !./scripts/inspect --json
```

Durations support seconds, minutes, hours, and days, such as `30s`, `15m`,
`1h`, and `1d`.

Task cache keys are content-addressed. They include task steps and options,
expanded `@use` function bodies, arguments, Promptfile variables, upstream
artifacts, provider and agent configuration, guidance, Docker configuration,
and referenced environment variables. Failed tasks and tasks that read
sensitive inputs are not cached. Cached step results are restored so
downstream artifacts remain equivalent to an uncached run.

## Run Replay

Successful and failed non-dry runs are stored locally with redacted prompts,
responses, step outputs, hosts, exit codes, provider names, and attempt
numbers:

```bash
makethlm history
makethlm replay 42
makethlm --json replay 42
```

Replay is read-only: it displays the recorded bundle and never executes the
task again.
