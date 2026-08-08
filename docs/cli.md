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
| `--capabilities` | | Explain transitive shell, SSH, Docker, LLM, secret, and webhook capabilities |
| `--plan` | | Preview execution order, variables, providers, hosts, and resolved steps |
| `--graph` | | Print a task dependency graph and exit |
| `--graph-format FORMAT` | | Graph format: `mermaid` or `dot` |
| `--history [N]` | | Show recent local run history and exit |
| `--no-history` | | Do not record this run in local history |
| `--evaluate EXPR` | | Evaluate an expression and print the result |
| `--dry-run` | | Print prompts and commands without executing them |
| `--json` | | Emit machine-readable JSON output |
| `--parallel` | | Run independent dependency tasks in parallel |
| `--jobs N` | | Limit parallel task workers; implies `--parallel` |
| `--always-make` | `-B` | Run tasks even when sources are unchanged or results are cached |
| `--since REF` | | Git ref that `changed()`/`changed_files()` compare against |
| `--watch` | | Re-run the target whenever a watched source file changes |
| `--watch-interval SECONDS` | | Polling interval for `--watch` (default: 1.0) |
| `--max-cost USD` | | Stop the run once LLM spend reaches this many US dollars |
| `--log-llm PATH` | | Append every LLM call to PATH as JSONL for live debugging |
| `--fixtures DIR` | | Serve LLM responses from recorded fixtures in DIR |
| `--record-fixtures` | | Call providers normally and record responses into `--fixtures DIR` |
| `--model MODEL` | `-m` | Override the LLM model for all tasks |
| `--var NAME=VALUE` | `-V` | Override a variable (can be repeated) |
| `--shell TEMPLATE` | | Use an LLM CLI argv template (e.g., `'ollama run llama3 "{prompt}"'`) |
| `--codex` | | Use the Codex CLI as the default LLM dispatcher |
| `--openai` | | Use the native OpenAI API dispatcher as the default LLM dispatcher |
| `--ollama` | | Use the native Ollama HTTP dispatcher as the default LLM dispatcher |
| `--opencode` | | Use the opencode CLI as the default LLM dispatcher |
| `--safe` | | Enable restrictive safety checks before execution |
| `--allow-backticks` | | Allow parse-time backtick commands in safe or inspection modes |
| `--allow-shell` | | Allow local shell steps in safe mode |
| `--allow-ssh` | | Allow SSH shell steps in safe mode |
| `--allow-docker` | | Allow docker blocks in safe mode |
| `--allow-llm` | | Allow LLM prompt execution in safe mode |
| `--allow-secrets` | | Allow secret reads and sensitive interpolation in safe mode |
| `--allow-mcp` | | Allow tasks to attach MCP servers in safe mode |
| `--allow-webhook` | | Allow webhook delivery in safe mode |
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
makethlm --capabilities deploy
makethlm --capabilities --json deploy

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
makethlm replay 42
makethlm --json replay 42

# Run with explicit safety permissions
makethlm --safe --allow-shell --allow-llm --allow-secrets test

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

## Formatting

`makethlm fmt` rewrites Promptfiles into a canonical layout:

```bash
makethlm fmt                 # format the discovered Promptfile
makethlm fmt ops/deploy.pf   # format specific files
makethlm fmt --check         # exit 1 if anything would change (for CI)
```

The formatter only touches layout: body indentation becomes four spaces,
relative indentation inside script recipes is preserved, trailing whitespace is
removed, runs of blank lines collapse to one, option brackets become
`[a=1, b=2]`, and the file ends with a single newline. Prompt prose, shell
commands, and the blank-line grouping you chose between declarations are left
alone.

## Promptfile Discovery

Resolution order:

1. `-f PATH`.
2. The first matching name in the current directory, then each parent:
   `Promptfile`, `promptfile`, `Promptfile.pf`, `promptfile.pf`, `.promptfile`,
   `.Promptfile`, `.promptfile.pf`, `.Promptfile.pf`, `PROMPTFILE`,
   `PROMPTFILE.pf`.
3. The same names under `$XDG_CONFIG_HOME/makethlm/`.
