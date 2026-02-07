# LLM Providers

makethlm supports multiple LLM providers. Declare them globally and optionally override per task.

## Declaring Providers

```
llm claude [model=opus]
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
