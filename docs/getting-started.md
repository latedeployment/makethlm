# Getting Started

## Installation

```bash
pip install makethlm
```

Requires Python 3.10 or newer.

By default, makethlm uses the [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) as its LLM backend. Make sure it is installed and authenticated:

```bash
claude --version
```

To use a different provider, see [LLM Providers](llm-providers.md).

## Your First Promptfile

Create a file named `Promptfile` in your project directory:

```
# Promptfile

project := "my-app"

llm claude [model=sonnet]

task hello:
    say hello and describe what {{project}} could be
```

Run it:

```bash
makethlm hello
```

The LLM receives the prompt "say hello and describe what my-app could be" and returns its response.

## Mixing Prompts and Shell Commands

The real power of makethlm is interleaving shell commands with LLM reasoning:

```
task analyze:
    !git diff --name-only > /tmp/changed.txt
    review the changed files listed in /tmp/changed.txt for security issues
    !npm test
    if any tests failed, explain the root cause
```

Lines starting with `!` are shell commands. Everything else is a prompt sent to the LLM. Shell commands and prompts alternate naturally -- run a command, reason about its output, run another command.

## Dependencies

Tasks can depend on other tasks:

```
task build:
    !npm run build

task test: build
    !npm test
    if any tests failed, explain the root cause

task deploy: build test
    deploy to production
```

Running `makethlm deploy` executes `build`, then `test`, then `deploy`.

## Task Arguments

Tasks can accept arguments:

```
task deploy(target, port="8080"):
    deploy {{project}} to {{target}} on port {{port}}
```

```bash
makethlm deploy staging        # target=staging, port=8080
makethlm deploy prod 443       # target=prod, port=443
```

## Dry Run

Preview what would happen without executing:

```bash
makethlm --dry-run deploy staging
```

## What's Next

- [Syntax Reference](syntax.md) -- full language reference
- [Variables](variables.md) -- variable system details
- [Shell Commands](shell-commands.md) -- shell command modifiers
- [Functions](functions.md) -- reusable prompt templates
- [LLM Providers](llm-providers.md) -- configure different LLMs
