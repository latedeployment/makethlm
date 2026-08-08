# LLM Providers

makethlm supports multiple LLM providers. Declare them globally and optionally override per task.

## Declaring Providers

```
llm claude [model=opus]
llm codex [model=gpt-5-codex]
llm openai [model=gpt-4, key=$OPENAI_API_KEY]
llm ollama [model=llama3, base_url=http://localhost:11434]
llm custom [template=my-cli {prompt}]
```

The **first declared provider** is the default for all tasks.

## Provider Options

| Option | Description |
|--------|-------------|
| `model` | Model name/ID to use |
| `key` | API key (prefix with `$` to read from an environment variable) |
| `base_url` | Custom API endpoint URL |
| `template` | Shell command template for custom CLI providers (see below) |

## Codex CLI

Codex is supported as a first-class CLI provider:

```
llm codex [model=gpt-5-codex]

task review [llm=codex]:
    review the current diff and suggest fixes
```

makethlm invokes `codex exec` non-interactively and sends the prompt on stdin.
Install and authenticate Codex first with `codex login`.

The Codex run uses `--json` and `--output-last-message`, which gives makethlm
three things a plain stdout read does not:

- **Token usage.** `turn.completed` carries `input_tokens` and `output_tokens`,
  so Codex calls are counted in the run's usage summary and history rather than
  being reported as unpriced.
- **A reliable answer.** The final message is read from the file Codex writes,
  not scraped out of whatever else the stream printed.
- **Native output contracts.** When a task declares `produces=object`, `array`,
  `json`, `integer`, `number`, or `boolean`, makethlm passes a matching JSON
  Schema via `--output-schema` so Codex constrains the answer at the source.
  `repair` still applies, but usually has nothing left to fix.

A Codex build that does not recognize these flags is detected and retried with
plain output, so older installs keep working.

## opencode

[opencode](https://opencode.ai) is supported as a CLI provider:

```
llm opencode [model=anthropic/claude-sonnet-4-5]

task review [llm=opencode]:
    review the current diff and suggest fixes
```

Models use opencode's `provider/model` form. makethlm runs
`opencode run --format json --auto` and reads the assistant text from the event
stream, falling back to raw stdout if the stream shape is unfamiliar.

Two things to know:

- **`--auto` is passed**, matching how the Claude and Codex dispatchers run
  non-interactively — without it a permission request would block until the task
  timed out. opencode is therefore treated as a local-execution provider, so a
  task using it needs `--allow-shell` as well as `--allow-llm` under `--safe`.
- **opencode reports no token usage**, so its calls count as `unpriced` unless
  the provider declares `price-in`/`price-out`.

## Native OpenAI

`llm openai` calls the OpenAI Chat Completions API directly. Set
`OPENAI_API_KEY` or pass `key=$OPENAI_API_KEY` in the provider declaration.

```
llm openai [model=gpt-4o-mini, key=$OPENAI_API_KEY]
```

Use `base_url` for compatible gateways.

## Native Ollama

`llm ollama` calls a local Ollama HTTP server directly:

```
llm ollama [model=llama3, base_url=http://127.0.0.1:11434]
```

## Custom CLI Providers

Use any LLM tool that accepts a prompt on the command line. The `{prompt}` placeholder is replaced with the actual prompt text:

```
llm custom [template=my-llm run {prompt}]
llm local [template=ollama run llama3 {prompt}]
```

## Per-Task Override

Use a different provider or model for specific tasks:

```
# Use opus for thorough security reviews
task review [llm=claude, model=opus]:
    @use security_review
    Focus on the current PR.

# Use a cheaper/faster model for linting
task lint [llm=openai]:
    @use code_quality
    Apply to src/.
```

## Retry and Fallback Strategy

Retry a provider and fall back to other declared providers:

```
llm openai [model=gpt-4o-mini, key=$OPENAI_API_KEY]
llm local [model=llama3, base_url=http://127.0.0.1:11434]

task review [llm=openai, retries=1, fallback-llm=local]:
    review the current diff
```

`retries` applies to each provider and is limited to 10. Up to four distinct
providers may be listed in `fallback-llm`; they are separated with `|`, tried in order, and recorded on the
prompt step together with the successful attempt number. Every fallback
receives the same resolved prompt, so review the provider chain with
`makethlm --capabilities TASK` before using secrets or sensitive source text.

## CLI Overrides

Override the model from the command line:

```bash
makethlm review --model sonnet
```

Or bypass all configured providers with an arbitrary CLI:

```bash
makethlm review --shell 'ollama run llama3 "{prompt}"'
```

To use Codex as the default dispatcher for a run:

```bash
makethlm review --codex
```

Native OpenAI, Ollama, and opencode can also be selected for one run:

```bash
makethlm review --openai -m gpt-4o-mini
makethlm review --ollama -m llama3
makethlm review --opencode -m anthropic/claude-sonnet-4-5
```

## Rate Limits and Concurrency

`--parallel` can put several tasks on one provider at once. Cap that per
provider:

```make
llm openai [model=gpt-4o, max-concurrency=2]
```

The cap is shared across all tasks in a run, so a fan-out of ten review tasks
still makes at most two simultaneous calls.

When a failed attempt looks like throttling — HTTP 429, "rate limit", "too many
requests", "quota exceeded", or "overloaded" — makethlm waits before the next
`retries` attempt, doubling from 2 seconds up to a 30-second cap. Failures that
are not throttling retry immediately, as before.

## Progress Output

A prompt that takes minutes is otherwise indistinguishable from a hang. On an
interactive terminal, makethlm shows a live elapsed timer next to the provider
it is waiting on. The line rewrites in place and is erased when the call
returns; when stderr is redirected nothing is printed, so logs stay clean.

## Multiple Models

### Fan-out

Send one prompt to several providers at once with a pipe-separated `llm`:

```make
llm claude [model=opus]
llm openai [model=gpt-4o]
llm local  [model=llama3]

task review [llm="claude|openai|local"]:
    review this diff for security bugs
```

All providers are called concurrently, subject to each provider's
`max-concurrency` and the run's `--max-cost`. Every answer is kept and
individually addressable:

```make
task compare: review:
    !echo "{{review.claude.response}}"
    !echo "{{review.openai.response}}"
```

The task's own response contains every answer, labeled by provider. A fan-out
step succeeds when at least one provider answered, matching how `fallback-llm`
treats one working provider as enough; providers that failed are still listed
in the output so nothing is hidden. Up to 8 providers may fan out.

### Judging

Add `judge` to merge the answers into one:

```make
task review [llm="claude|openai|local", judge=claude]:
    review this diff for security bugs
```

The judge receives the original task and every answer, and is asked to prefer
what the models agree on and drop what is contradicted. Its answer becomes the
task's response; the individual answers remain available as
`{{review.claude.response}}` and friends. If the judge itself fails, the task
falls back to reporting all answers rather than losing them. `judge` requires a
fan-out — it has nothing to merge otherwise.

### Chaining models

`@llm <name>` sets the provider for the prompt steps that follow it, and a
prompt line ending in `|>` pipes its answer into the next prompt:

```make
task notes:
    @llm local
    draft the release notes from the changelog |>
    @llm claude
    tighten the wording and fix any inaccuracies
```

This is the same piping `!cmd |>` already does for shell output, so a chain can
mix both. A bare `@llm` clears the override and returns to the task's provider.
A step-level `@llm` also wins over a task-level fan-out, so one step in a
fan-out task can be pinned to a single model.

## MCP Servers

Declare an MCP server once and attach it to the tasks that need it:

```make
mcp files [command="npx -y @modelcontextprotocol/server-filesystem /tmp"]
mcp github [url=https://api.githubcopilot.com/mcp/]
mcp db [command=db-mcp, env(DB_URL, "postgres://localhost/app")]

task review [mcp="files|github"]:
    review the open pull request against the working tree
```

A declaration needs either `command=` (a local stdio server) or `url=` (a
remote one), never both. Extra environment variables use the same `env(NAME,
VALUE)` form as task options. Attach servers with `mcp=`, separated by pipes or
commas.

Each provider is configured **for that invocation only** — makethlm never edits
your global MCP configuration:

| Provider | How the servers are passed |
|----------|----------------------------|
| Claude | `--mcp-config` with an inline JSON document |
| Codex | `-c mcp_servers.<name>...` config overrides |
| opencode | `OPENCODE_CONFIG_CONTENT` inline JSON |

Attaching a server is a capability, so `--safe` requires `--allow-mcp`:

```bash
makethlm --capabilities review
#   mcp  review: attaches MCP server 'github' at https://api.githubcopilot.com/mcp/ (--allow-mcp)

makethlm --safe --allow-llm --allow-shell --allow-mcp review
```

Native OpenAI and Ollama providers have no MCP transport; a task attaching
servers and running on them simply does not get the tools.
