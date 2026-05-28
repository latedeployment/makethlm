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
| `--check` | | Validate Promptfile references, required tools, and risky capabilities |
| `--plan` | | Preview execution order, variables, providers, hosts, and resolved steps |
| `--graph` | | Print a task dependency graph and exit |
| `--graph-format FORMAT` | | Graph format: `mermaid` or `dot` |
| `--history [N]` | | Show recent local run history and exit |
| `--no-history` | | Do not record this run in local history |
| `--serve [HOST:PORT]` | | Serve a small local task UI/API |
| `--evaluate EXPR` | | Evaluate an expression and print the result |
| `--dry-run` | | Print prompts and commands without executing them |
| `--json` | | Emit machine-readable JSON output |
| `--parallel` | | Run independent dependency tasks in parallel |
| `--jobs N` | | Limit parallel task workers; implies `--parallel` |
| `--model MODEL` | `-m` | Override the LLM model for all tasks |
| `--var NAME=VALUE` | `-V` | Override a variable (can be repeated) |
| `--shell TEMPLATE` | | Use an arbitrary LLM CLI template (e.g., `'ollama run llama3 "{prompt}"'`) |
| `--codex` | | Use the Codex CLI as the default LLM dispatcher |
| `--openai` | | Use the native OpenAI API dispatcher as the default LLM dispatcher |
| `--ollama` | | Use the native Ollama HTTP dispatcher as the default LLM dispatcher |
| `--safe` | | Enable restrictive safety checks before execution |
| `--allow-backticks` | | Allow parse-time backtick command substitution in safe mode |
| `--allow-shell` | | Allow local shell steps in safe mode |
| `--allow-ssh` | | Allow SSH shell steps in safe mode |
| `--allow-docker` | | Allow docker blocks in safe mode |
| `--allow-llm` | | Allow LLM prompt execution in safe mode |
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

# Validate without executing
makethlm --check
makethlm --check --json

# Emit machine-readable output
makethlm --json deploy
makethlm --plan --json deploy
makethlm --graph --json deploy
makethlm history --json

# Run independent dependencies concurrently
makethlm --parallel deploy
makethlm --jobs 4 deploy

# Preview the execution plan without running anything
makethlm --plan deploy staging

# Print dependency graphs
makethlm --graph deploy
makethlm --graph --graph-format dot deploy

# Show local run history
makethlm history
makethlm --history 50

# Run with explicit safety permissions
makethlm --safe --allow-shell --allow-llm test

# Start the local self-hosted UI/API
makethlm --serve 127.0.0.1:8765

# Use a different model
makethlm review -m sonnet

# Use a different Promptfile
makethlm -f ops/Promptfile.pf deploy

# Use any LLM CLI
makethlm review --shell 'ollama run llama3 "{prompt}"'

# Use Codex CLI
makethlm review --codex

# Use native OpenAI or Ollama
makethlm review --openai -m gpt-4o-mini
makethlm review --ollama -m llama3

# Print shell completions
makethlm completions bash

# List everything
makethlm --list
```

Parallel execution runs tasks at the same dependency depth concurrently. If any
task in a level fails, later levels are skipped and the run fails.

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
