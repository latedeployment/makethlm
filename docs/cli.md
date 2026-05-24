# CLI Reference

```
makethlm [OPTIONS] [TASK] [ARGS...]
```

## Positional Arguments

| Argument | Description |
|----------|-------------|
| `TASK` | Task to run (default: first task in file) |
| `ARGS` | Positional arguments passed to the task |

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--file PATH` | `-f` | Path to Promptfile (default: auto-detect in current directory) |
| `--list` | `-l` | List all tasks, functions, LLM providers, and host groups |
| `--summary` | `-s` | List task names only (compact, one per line) |
| `--dump` | | Dump parsed Promptfile structure (variables, settings, tasks) |
| `--evaluate EXPR` | | Evaluate an expression and print the result |
| `--dry-run` | | Print prompts and commands without executing them |
| `--model MODEL` | `-m` | Override the LLM model for all tasks |
| `--var NAME=VALUE` | `-V` | Override a variable (can be repeated) |
| `--shell TEMPLATE` | | Use an arbitrary LLM CLI template (e.g., `'ollama run llama3 "{prompt}"'`) |
| `--codex` | | Use the Codex CLI as the default LLM dispatcher |
| `--quiet` | `-q` | Suppress command echoing |
| `--verbose` | | Verbose output with step details |

## Examples

```bash
# Run the default task
makethlm

# Run a specific task
makethlm deploy

# Run with arguments
makethlm deploy staging 443

# Override a variable
makethlm deploy -V env=production

# Preview what would happen
makethlm --dry-run deploy staging

# Use a different model
makethlm review -m sonnet

# Use a different Promptfile
makethlm -f ops/Promptfile.pf deploy

# Use any LLM CLI
makethlm review --shell 'ollama run llama3 "{prompt}"'

# Use Codex CLI
makethlm review --codex

# List everything
makethlm --list
```

## `--list` Output

The `--list` flag shows tasks (with dependencies, arguments, options), functions, LLM providers (with the default marked), and host groups:

```
$ makethlm --list
  build
    check if moo.md is newer than the Dockerfile...
  test (depends: build)
    run all tests
  review (args: scope; llm: claude)
    Review code for security vulnerabilities...
  deploy (args: target, port="8080"; depends: build, test; on: web)
    deploy my-web-app to target on port port...

  functions:
    security_review: Review the code for security vulnerabilities...
    code_quality: Check for code quality issues...

  llm providers:
    claude model=opus (default)
    openai model=gpt-4

  host groups:
    web user=deploy: web1.prod.internal, web2.prod.internal
    db user=postgres port=5433: db-primary.prod.internal
```
