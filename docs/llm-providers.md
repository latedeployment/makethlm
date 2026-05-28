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

Native OpenAI and Ollama can also be selected for one run:

```bash
makethlm review --openai -m gpt-4o-mini
makethlm review --ollama -m llama3
```
