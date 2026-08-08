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

## Contract Repair

An LLM that answers with prose around its JSON has not failed in a way a retry
against the same prompt will fix. `repair` re-prompts with the violation
attached instead:

```make
task inspect [produces=object, repair=1]:
    return the deployment report as a JSON object
```

The repair prompt restates the original prompt, names the contract, echoes the
rejected response (truncated at 2000 characters), and asks for corrected output
only. Repairs run up to 3 times and are off by default, so no task starts
spending extra LLM calls without opting in.

Only the task's final prompt step is repaired, and only that step is
re-dispatched: shell, SSH, and Docker steps never run twice because of a
repair. Repair attempts increment the prompt step's `attempt` counter. When the
budget is exhausted the task still fails closed on the contract.

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

## File Staleness

`sources` and `outputs` give tasks make-style incremental behavior. When every
output exists and is at least as new as every matched source, the task is
skipped:

```make
task build [sources="src/*.c, include/*.h", outputs="build/app"]:
    !mkdir -p build
    !cc -o build/app src/*.c
```

Patterns are separated by commas or pipes, support `*`, `?`, `[...]`, and
recursive `**`, and resolve against the task's working directory. Only regular
files participate; directories are ignored.

A task runs rather than skips when any of these hold, so an unclear state never
silently reuses stale output:

- Only one of `sources` and `outputs` is set.
- No source file matched the patterns.
- A literal (non-glob) output path does not exist.
- The newest source is newer than the oldest output.

Skipped tasks still record an artifact, with `success` set to `skipped`, so
dependents and `when` conditions can observe them.

`sources` also contributes a content digest to the task's cache key, so a
`cache` duration expires as soon as an input file changes on disk:

```make
task audit [sources="src/**/*.py", cache="1h"]:
    review the source tree for unsafe subprocess usage
```

Use `--always-make` (`-B`) to ignore both staleness and cache skips for a run.
`--dry-run` never skips, and `--plan` reports which tasks would be skipped.

`--watch` re-runs a target whenever a watched file changes:

```bash
makethlm --watch build
makethlm --watch --watch-interval 0.5 build
```

The watch set is the union of `sources` patterns across the target's dependency
closure, plus the Promptfile itself, so editing the Promptfile also triggers a
run. Watching a target whose closure declares no `sources` is an error rather
than a silent no-op. Normal staleness still applies between runs, so only the
tasks whose inputs actually changed do work.

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

## Cost and Budgets

Every LLM call records what it used. Native OpenAI and Ollama providers report
token counts, and the Claude CLI reports usage and spend directly. Declare
prices on any other provider to have spend derived from its token counts:

```make
llm openai [model=gpt-4o, price-in=2.50, price-out=10.00]
```

Prices are US dollars per million tokens. A run prints a usage line on stderr
when anything was recorded:

```
usage: 3 LLM calls, 12,400 in / 2,100 out tokens, $0.0521
```

Calls whose spend cannot be determined are counted as `unpriced` rather than
silently treated as free.

Stop a run before it spends more than you intend:

```bash
makethlm --max-cost 2.50 review
```

```make
task review [max-cost="0.50"]:
    review the release diff
```

The budget is a stop-loss, not a pre-authorization: spend is known only after a
call returns, so makethlm checks the running total before each dispatch and
fails the task closed once the limit is reached. The per-task `max-cost` and the
run-wide `--max-cost` both apply, and the lower one wins.

Token counts, spend, and call counts are stored in run history and included in
`--json` output. Replayed fixtures cost nothing and are recorded as such.

## Recorded Fixtures

Run history explains what happened. Fixtures let a Promptfile be *re-run*
without a provider, so prompt-driven workflows can be tested in CI with no
credentials, no network, and no spend.

Record once against a real provider:

```bash
makethlm --fixtures tests/fixtures --record-fixtures review
```

Then replay anywhere:

```bash
makethlm --fixtures tests/fixtures review
```

During replay no provider is called. Each fixture is keyed by task name and
redacted prompt text, so a changed prompt intentionally misses its fixture.
A miss fails the task closed with an explanation rather than silently falling
back to a live call, which keeps a CI run from quietly spending money.

Prompts and responses are redacted before they are written, and fixture files
are created atomically with owner-only permissions. Recorded failures replay as
failures, so error handling stays testable. Repair prompts are recorded as
their own fixtures, so a `produces`/`repair` sequence replays exactly as it
first ran.

Shell, SSH, and Docker steps still execute during replay; only LLM calls are
served from fixtures.

## Watching and Debugging LLM Calls

`--log-llm PATH` appends one JSON object per LLM call as it happens, so a long
run can be watched live:

```bash
makethlm --log-llm calls.jsonl deploy &
tail -f calls.jsonl | jq -c '{task, provider, kind, success, duration_ms}'
```

Each record carries the task, provider, call kind, attempt index, success,
duration, token counts, cost, and the redacted prompt and response. `kind`
explains *why* the call happened:

| `kind` | Meaning |
|--------|---------|
| `prompt` | An ordinary prompt step |
| `repair` | A re-prompt after a `produces` violation |
| `fanout` | One branch of an `llm="a|b"` fan-out |
| `judge` | The `judge` provider merging fan-out answers |
| `budget` | A call refused because `max-cost` was reached |

`source` distinguishes a real provider call from a replayed fixture, so a CI run
using `--fixtures` is visibly spending nothing. Prompts and responses are
redacted and truncated at 4000 characters, the file is created owner-only, and a
log that cannot be written never fails the run it was observing.

This is complementary to history: history records what a task produced, the call
log records every attempt it took to get there.
