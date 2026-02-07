# Promptfile

**A task runner where tasks are LLM prompts.**

[![PyPI version](https://img.shields.io/pypi/v/promptfile)](https://pypi.org/project/promptfile/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Promptfile is a command-line task runner in the tradition of Make and Just, but
designed for a world where tasks are described in natural language and executed
by LLMs. Define your build, deploy, review, and maintenance workflows as
prose, interleave them with shell commands, and let your LLM of choice do the
heavy lifting.

```
# Promptfile

project := "my-web-app"

llm claude [model=opus]

task build:
    !mkdir -p dist
    check if src/ has changed since the last build.
    if so, compile the TypeScript and bundle with esbuild.

task test: build:
    !npm test
    if any tests failed, explain the root cause and suggest a fix.

task deploy(target, port="8080"): build test:
    !systemctl restart {{project}}
    verify {{project}} is running on {{target}} port {{port}}.
```

```
$ promptfile deploy staging
[ok] build
  ...
[ok] test
  ...
[ok] deploy
  Verified my-web-app is running on staging port 8080. All health checks pass.
```

---

## Table of Contents

- [Why Promptfile?](#why-promptfile)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Promptfile Syntax Reference](#promptfile-syntax-reference)
  - [Comments](#comments)
  - [Variables](#variables)
  - [Tasks](#tasks)
  - [Dependencies](#dependencies)
  - [Task Arguments](#task-arguments)
  - [Shell Commands](#shell-commands)
  - [Functions](#functions)
  - [Docker Support](#docker-support)
  - [LLM Provider Selection](#llm-provider-selection)
  - [Host Inventory (SSH)](#host-inventory-ssh)
  - [Includes](#includes)
  - [Environment Variables](#environment-variables)
  - [Task Metadata Options](#task-metadata-options)
  - [Set Directives](#set-directives)
  - [Aliases](#aliases)
- [CLI Reference](#cli-reference)
- [Comparison with Make and Just](#comparison-with-make-and-just)
- [Complete Example](#complete-example)

---

## Why Promptfile?

Modern development workflows increasingly rely on LLMs for code review,
documentation, refactoring, and operational reasoning. But invoking an LLM from
a script usually means gluing together shell commands, API calls, prompt
templates, and ad hoc variable substitution.

Promptfile gives these workflows a proper home. It provides:

- A **declarative file format** purpose-built for LLM-driven tasks.
- **Shell interleaving** so you can mix `npm test` with "explain the failures."
- **Dependency resolution** so `deploy` automatically runs `build` and `test` first.
- **Multi-provider support** -- Claude, OpenAI, Ollama, or any CLI -- switchable per task.
- **Remote execution** with an Ansible-like host inventory for SSH-based deploys.
- **Docker generation** -- describe an image in prose and let the LLM write the Dockerfile.

---

## Installation

```bash
pip install promptfile
```

Requires Python 3.10 or newer.

By default, Promptfile uses the [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) as its LLM backend. Make sure it is installed and authenticated:

```bash
claude --version
```

To use a different provider, see [LLM Provider Selection](#llm-provider-selection).

---

## Quick Start

1. **Create a `Promptfile`** in your project root:

```
project := "my-app"

task review:
    Review the code in src/ for security vulnerabilities.
    Check for SQL injection, XSS, and command injection.
    Be concise and actionable.
```

2. **Run it:**

```bash
promptfile review
```

3. **Or just run the default** (the first task defined):

```bash
promptfile
```

4. **Preview without executing:**

```bash
promptfile --dry-run review
```

5. **List all tasks:**

```bash
promptfile --list
```

---

## How It Works

When you run `promptfile <task>`, the following happens:

```
                       Promptfile
                      +---------------------+
                      | variables, LLMs,    |
                      | hosts, functions,   |
                      | tasks               |
                      +----------+----------+
                                 |
                          1. Parse file
                                 |
                                 v
                      +----------+----------+
                      | Resolve target task |
                      | and dependencies    |
                      +----------+----------+
                                 |
                     2. Topological sort
                        (dependency order)
                                 |
                                 v
               +----------------------------------+
               | For each task in order:          |
               |                                  |
               |   For each step in the task:     |
               |                                  |
               |     Shell step (!command)         |
               |       --> subprocess.run()        |
               |       --> if [on=hosts], via SSH  |
               |                                  |
               |     Prompt step (natural lang.)  |
               |       --> expand @use functions  |
               |       --> interpolate {{vars}}   |
               |       --> resolve ${ENV_VARS}    |
               |       --> dispatch to LLM        |
               |                                  |
               |   Stop on first failure          |
               +----------------------------------+
                                 |
                                 v
                        Print results
```

Key behaviors:

- **Dependencies run first.** If `deploy` depends on `build` and `test`, those
  run (in topological order) before `deploy` begins.
- **Steps execute sequentially.** Within a task, shell commands and LLM prompts
  alternate in the order they appear.
- **Failure halts execution.** A non-zero shell exit or LLM error stops the
  current task and all downstream tasks. Use `@ignore` to override this for
  individual shell commands.
- **Arguments are scoped.** CLI arguments are only passed to the target task,
  not to its dependencies.

---

## Promptfile Syntax Reference

Promptfile looks for a file named `Promptfile`, `promptfile`, `Promptfile.pf`,
or `promptfile.pf` in the current directory (or specify one with `-f`).

### Comments

Lines starting with `#` are comments and are ignored by the parser.

```
# This is a comment
task build:
    build the project  # inline text is NOT a comment -- this is part of the prompt
```

### Variables

Define variables with `:=` and reference them with `{{name}}` in prompts and
shell commands.

```
project := "my-web-app"
env := "staging"

task deploy:
    deploy {{project}} to {{env}}
```

Variable values must be double-quoted strings. Escaped quotes (`\"`) and
escaped backslashes (`\\`) are supported inside the value:

```
greeting := "hello \"world\""
```

**Backtick variables** execute a shell command at parse time and capture its
stdout:

```
version := `git describe --tags`

task release:
    release {{version}} to production
```

Variables can be overridden from the CLI with `--var` / `-V`:

```bash
promptfile deploy -V env=production
```

### Tasks

A task is defined with the `task` keyword, a name, and a colon. The indented
body that follows is a mix of LLM prompts (natural language) and shell commands.

```
task build:
    !mkdir -p dist
    check if moo.md is newer than the Dockerfile.
    if so, rebuild the docker image from scratch.
    tag it as {{project}}:latest.
```

The **first task** defined in the file is the **default task**. Running
`promptfile` with no arguments executes it.

Consecutive lines of natural language are merged into a single prompt and sent
to the LLM together. Shell commands (lines starting with `!`) break prompt
boundaries, so LLM prompts before and after a shell command become separate LLM
calls.

### Dependencies

A task can depend on other tasks. Dependencies are listed between the task name
and the final colon:

```
task deploy: build test:
    deploy to production
```

This means: run `build` first, then `test`, then `deploy`. Dependencies are
resolved via topological sort, so transitive dependencies and diamond
dependencies work correctly. Cycles are detected and reported as errors.

```
task a:
    do a

task b: a:
    do b

task c: a:
    do c

task d: b c:
    do d
```

Running `promptfile d` executes: `a`, then `b` and `c` (in dependency order),
then `d`.

### Task Arguments

Tasks can accept positional arguments with optional defaults:

```
task deploy(target, port="8080"):
    deploy {{project}} to {{target}} on port {{port}}
```

Pass arguments on the command line after the task name:

```bash
promptfile deploy staging        # target=staging, port=8080 (default)
promptfile deploy prod 443       # target=prod, port=443
```

Arguments are interpolated into both prompt text and shell commands via the
same `{{name}}` syntax as variables. If a required argument (one without a
default) is not provided, Promptfile exits with an error.

Arguments are **scoped to the target task** -- they are not passed to
dependency tasks.

### Shell Commands

Lines starting with `!` are shell commands executed as subprocesses:

```
task setup:
    !mkdir -p dist
    !npm install
    !npm run build
```

Shell commands support two modifier prefixes:

| Prefix | Effect |
|--------|--------|
| `@silent` | Suppress the command's stdout/stderr output |
| `@ignore` | Continue execution even if the command exits non-zero |

Modifiers are placed between `!` and the command:

```
task clean:
    !@silent rm -rf dist/
    !@ignore docker rmi old-image:latest
    !@silent @ignore docker system prune -f
```

Shell commands and LLM prompts can be **freely interleaved** in a single task.
This is one of Promptfile's defining features -- run a command, reason about
its output, run another command:

```
task analyze:
    !git diff --name-only > /tmp/changed.txt
    review the changed files listed in /tmp/changed.txt for security issues
    !npm test
    if any tests failed, explain the root cause
```

Variable interpolation (`{{name}}`) works inside shell commands:

```
project := "myapp"

task build:
    !docker build -t {{project}}:latest .
```

### Functions

Functions are reusable prompt templates defined with `fn`. They are injected
into task bodies with the `@use` directive.

```
fn security_review:
    Review the code for security vulnerabilities.
    Check specifically for:
    - SQL injection
    - XSS (cross-site scripting)
    - Command injection
    - Path traversal
    Be concise and actionable.

fn code_quality:
    Check for code quality issues:
    - Functions longer than 50 lines
    - Duplicated logic
    - Missing error handling

task review:
    @use security_review
    Focus on the git diff for the current PR.

task full-review:
    @use security_review
    @use code_quality
    Apply to the entire src/ directory.
```

When a task is executed, every `@use name` line is replaced with the full text
of the named function. Multiple `@use` directives can appear in the same task.
Functions cannot themselves contain `@use` (no recursive expansion).

### Docker Support

The `docker` block lets you describe a Docker image in natural language. The
LLM generates a Dockerfile, and Promptfile builds it automatically.

```
docker api-server [tag=latest]:
    A Python 3.11 slim image.
    Install requirements.txt with pip, no cache.
    Copy the app/ directory to /app.
    Set the working directory to /app.
    Expose port 8080.
    Run with gunicorn, 4 workers, binding 0.0.0.0:8080.
```

Running `promptfile api-server` will:

1. Send the description to the LLM with instructions to output a raw Dockerfile.
2. Write the generated Dockerfile to the configured path.
3. Run `docker build` with the specified tag and context.

Docker blocks accept the following options:

| Option | Default | Description |
|--------|---------|-------------|
| `tag` | `latest` | Image tag |
| `context` | `.` | Build context directory |
| `file` | `Dockerfile` | Dockerfile path |

```
docker frontend [tag=v2, context=./client, file=Dockerfile.prod]:
    Node 20 alpine image.
    Run npm ci, then npm run build.
    Serve with nginx on port 80.
```

Docker blocks appear in the task list and can be used as dependencies:

```
docker api:
    Python 3.11 slim image. Install requirements.txt.

task deploy: api:
    push the api image to the registry
```

### LLM Provider Selection

Promptfile supports multiple LLM providers. Declare them globally and
optionally override per task.

```
llm claude [model=opus]
llm openai [model=gpt-4, key=$OPENAI_API_KEY]
llm ollama [model=llama3, base_url=http://localhost:11434]
llm custom [template=my-cli {prompt}]
```

The **first declared provider** is the default for all tasks.

**Provider options:**

| Option | Description |
|--------|-------------|
| `model` | Model name/ID to use |
| `key` | API key (prefix with `$` to read from an environment variable) |
| `base_url` | Custom API endpoint URL |
| `template` | Shell command template for custom CLI providers (see below) |

**Custom CLI providers** let you use any LLM tool that accepts a prompt on the
command line. The `{prompt}` placeholder is replaced with the actual prompt
text:

```
llm custom [template=my-llm run {prompt}]
llm local [template=ollama run llama3 {prompt}]
```

**Per-task override** -- use a different provider (or model) for specific tasks:

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

You can also override the model from the CLI:

```bash
promptfile review --model sonnet
```

Or bypass all configured providers and use an arbitrary CLI:

```bash
promptfile review --shell 'ollama run llama3 "{prompt}"'
```

### Host Inventory (SSH)

Promptfile includes an Ansible-like host inventory for running shell commands
on remote machines via SSH.

**Define host groups:**

```
hosts web [user=deploy, port=22]:
    web1.prod.internal
    web2.prod.internal
    web3.prod.internal

hosts db [user=postgres, port=5433]:
    db-primary.prod.internal
    db-replica.prod.internal
```

**Target a host group from a task:**

```
task deploy [on=web]:
    !systemctl restart my-web-app
    verify the app is responding on port 8080
```

When a task has `[on=<group>]`:

- **Shell commands** (`!` lines) execute on **every host** in the group via
  SSH, sequentially. If any host fails, execution stops.
- **Prompt steps** (natural language) still execute **locally** via the LLM.

This lets you interleave remote operations with local LLM reasoning:

```
task deploy [on=web]:
    !systemctl restart myapp          # runs on each web host via SSH
    verify the restart was successful  # runs locally via LLM
    !curl -sf http://localhost/health  # runs on each web host via SSH
```

**Host group options:**

| Option | Default | Description |
|--------|---------|-------------|
| `user` | (none -- uses SSH default) | SSH username |
| `port` | (none -- uses SSH default of 22) | SSH port |

SSH connections use `BatchMode=yes` for non-interactive operation.

### Includes

Split large Promptfiles across multiple files with the `include` directive:

```
include "common/variables.pf"
include "tasks/deploy.pf"
include "tasks/docker.pf"
```

Included files are resolved relative to the file containing the `include`
statement. All definitions (variables, functions, tasks, LLM providers, host
groups) from the included file are merged into the current file.

**Override precedence:** If both the included file and the including file
define the same variable, function, or task, the **local definition wins**.

**Circular include detection:** If file A includes file B, and file B includes
file A, the parser raises an error.

```
# common.pf
env := "default"

fn preamble:
    You are a helpful assistant.

# Promptfile
include "common.pf"
env := "production"       # overrides the included value

task greet:
    @use preamble
    Deploy to {{env}}     # resolves to "production"
```

### Environment Variables

Reference environment variables in prompts with `${VAR}` syntax. An optional
default value can be provided with `:-`:

```
task deploy:
    deploy to ${DEPLOY_TARGET}
    use credentials from ${SECRET_PATH:-/etc/secrets/default}
```

| Syntax | Behavior |
|--------|----------|
| `${VAR}` | Replaced with the value of `$VAR`, or empty string if unset |
| `${VAR:-fallback}` | Replaced with `$VAR` if set, otherwise `fallback` |

Environment variables are resolved in **prompt steps only** (not in shell
commands, where the shell itself handles `$VAR` expansion).

### Task Metadata Options

Tasks accept metadata options in square brackets. These control LLM behavior,
execution targets, and CLI presentation.

```
task review [llm=claude, model=opus, temperature=0.2, max_tokens=4096]:
    review the code carefully
```

**Full option reference:**

| Option | Type | Description |
|--------|------|-------------|
| `model` | string | LLM model to use for this task |
| `temperature` | float | Sampling temperature (e.g., `0.2` for deterministic, `0.9` for creative) |
| `max_tokens` | int | Maximum tokens in the LLM response |
| `llm` | string | Name of the LLM provider to use (must be declared globally) |
| `on` | string | Host group to execute shell commands on via SSH |
| `private` | flag | Hide this task from `--list` output (also: `_`-prefixed tasks) |
| `group` | string | Group heading for `--list` (e.g., `group="deploy"`) |
| `doc` | string | Description shown in `--list` output |
| `confirm` | flag/string | Prompt for confirmation before running. Use `confirm` for the default message or `confirm="Are you sure?"` for a custom one |
| `os` | string | Only run this task on the specified OS (e.g., `os=linux`) |
| `linux` | flag | Shorthand for `os=linux` (Justfile-compatible) |
| `macos` | flag | Shorthand for `os=macos` (Justfile-compatible) |
| `windows` | flag | Shorthand for `os=windows` (Justfile-compatible) |
| `unix` | flag | Shorthand for `os=unix` (matches Linux and macOS) |
| `working-dir` | string | Change to this directory before executing the task |
| `no-cd` | flag | Don't change to working directory (overrides `set working-dir`) |
| `no-exit-message` | flag | Suppress error message on failure |
| `no-quiet` | flag | Override global `set quiet` for this task |
| `positional-arguments` | flag | Per-task override for positional argument passing |

Options can be combined with dependencies:

```
task deploy(target): build test [llm=openai, on=web, model=gpt-4]:
    deploy {{project}} to {{target}}
```

### Set Directives

Global configuration directives that affect the entire Promptfile:

```
set dotenv-load
set shell "bash"
set working-dir "/home/deploy/app"
```

| Directive | Description |
|-----------|-------------|
| `set dotenv-load` | Automatically load a `.env` file from the working directory |
| `set dotenv-path "path"` | Custom `.env` file path |
| `set dotenv-required` | Error if `.env` is missing |
| `set shell "name"` | Set the shell used for `!` commands (default: system shell) |
| `set working-dir "path"` | Set the working directory for all tasks |
| `set export` | Export all variables to the environment |
| `set positional-arguments` | Pass task arguments as `$1`, `$2`, etc. |
| `set fallback` | Search parent directories for a Promptfile |
| `set ignore-comments` | Strip `#` comments from shell commands |
| `set quiet` | Suppress command echoing globally |
| `set tempdir "path"` | Temporary directory for recipes |
| `set allow-duplicate-tasks` | Allow redefining tasks (last wins) |
| `set allow-duplicate-variables` | Allow redefining variables |

### Export Variables

Variables prefixed with `export` are passed to the environment of shell commands:

```
export API_KEY := "secret"
export DATABASE_URL := "postgres://..."

task deploy:
    !echo $API_KEY          # accessible in shell commands
    deploy with API key
```

Use `set export` to export **all** variables:

```
set export

project := "myapp"         # exported automatically
version := "1.0"           # exported automatically
```

### String Concatenation

Variables support string concatenation with `+`:

```
prefix := "my"
project := prefix + "-app" + "-v1"     # "my-app-v1"
```

### Conditional Expressions

Justfile-compatible `if/else` expressions in variables and templates:

```
env := "production"
message := if env == "production" { "deploy carefully" } else { "test freely" }

task deploy:
    {{if env == "production" { "running production deploy" } else { "running dev deploy" }}}
```

Operators: `==`, `!=`.

### Built-in Functions

Justfile-compatible built-in functions, available in `{{ }}` templates:

| Function | Description |
|----------|-------------|
| `{{os()}}` | Current OS: `linux`, `macos`, or `windows` |
| `{{os_family()}}` | OS family: `unix` or `windows` |
| `{{arch()}}` | CPU architecture (e.g., `x86_64`, `aarch64`) |
| `{{num_cpus()}}` | Number of CPU cores |
| `{{home_directory()}}` | User's home directory |

```
task info:
    running on {{os()}} ({{arch()}}) with {{num_cpus()}} cores
    home: {{home_directory()}}
```

### Variadic Arguments

Tasks can accept variadic arguments (Justfile-compatible):

```
# One or more (required)
task deploy(+targets):
    deploy to {{targets}}

# Zero or more (optional)
task greet(*names):
    hello {{names}}
```

```bash
promptfile deploy staging prod     # targets="staging prod"
promptfile greet alice bob         # names="alice bob"
promptfile greet                   # names="" (ok for *)
```

### Line Continuation

Long lines can be split with `\`:

```
task build:
    this is a very long prompt \
    that continues on the next line
```

### Aliases

Create short aliases for tasks:

```
alias d := deploy
alias t := test
alias r := review
```

After defining an alias, `promptfile d` is equivalent to `promptfile deploy`.

---

## CLI Reference

```
promptfile [OPTIONS] [TASK] [ARGS...]
```

**Positional arguments:**

| Argument | Description |
|----------|-------------|
| `TASK` | Task to run (default: first task in file) |
| `ARGS` | Positional arguments passed to the task |

**Options:**

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
| `--quiet` | `-q` | Suppress command echoing |
| `--verbose` | | Verbose output with step details |

**Examples:**

```bash
# Run the default task
promptfile

# Run a specific task
promptfile deploy

# Run with arguments
promptfile deploy staging 443

# Override a variable
promptfile deploy -V env=production

# Preview what would happen
promptfile --dry-run deploy staging

# Use a different model
promptfile review -m sonnet

# Use a different Promptfile
promptfile -f ops/Promptfile.pf deploy

# Use any LLM CLI
promptfile review --shell 'ollama run llama3 "{prompt}"'

# List everything
promptfile --list
```

**`--list` output** includes tasks (with dependencies, arguments, options),
functions, LLM providers (with the default marked), and host groups:

```
$ promptfile --list
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

---

## Comparison with Make and Just

| Feature | Make | Just | Promptfile |
|---------|------|------|------------|
| Task definitions | Targets with recipes | Recipes with commands | Tasks with prompts + commands |
| Task body language | Shell commands | Shell commands | Natural language + shell |
| LLM integration | None | None | First-class, multi-provider |
| Variable interpolation | `$(VAR)` | `{{VAR}}` | `{{VAR}}` |
| String concatenation | N/A | `+` operator | `+` operator |
| Conditional expressions | N/A | `if/else` | `if/else` |
| Built-in functions | N/A | `os()`, `arch()`, etc. | `os()`, `arch()`, etc. |
| Environment variables | `$$VAR` | `$VAR` | `${VAR}` with defaults |
| Export variables | `export` | `export` | `export` / `set export` |
| Dependencies | File-based (mtime) | Task-based | Task-based (topological) |
| Task arguments | None | Positional + defaults + variadic | Positional + defaults + variadic |
| Shell command prefix | (tab-indented) | (indented) | `!` prefix |
| Reusable templates | None | None | `fn` / `@use` |
| Docker generation | None | None | `docker` blocks |
| Remote execution | None | None | SSH host inventory |
| Multi-LLM routing | N/A | N/A | Per-task `[llm=...]` |
| OS-specific tasks | N/A | `[linux]`, `[macos]` | `[linux]`, `[macos]`, `[unix]` |
| Private tasks | N/A | `_` prefix / `[private]` | `_` prefix / `[private]` |
| Confirmation | N/A | `[confirm]` | `[confirm]` / `[confirm="msg"]` |
| Line continuation | `\` | `\` | `\` |
| Quiet mode | N/A | `@` prefix / `set quiet` | `@` prefix / `set quiet` |
| Fallback search | N/A | `set fallback` | `set fallback` |
| File composition | `include` | `import` | `include` |
| Dry run | `-n` flag | `--dry-run` | `--dry-run` |
| Dump structure | N/A | `--dump` | `--dump` |
| File name | `Makefile` | `justfile` | `Promptfile` |

---

## Complete Example

Below is a realistic Promptfile for a web application project that uses
multiple LLM providers, host groups, functions, Docker, and all major features:

```
# ==========================================================================
# Promptfile for my-web-app
# ==========================================================================

project := "my-web-app"
env := "staging"

# --- LLM Providers --------------------------------------------------------

llm claude [model=opus]
llm openai [model=gpt-4, key=$OPENAI_API_KEY]
llm ollama [model=llama3, base_url=http://localhost:11434]

# --- Host Inventory -------------------------------------------------------

hosts web [user=deploy]:
    web1.prod.internal
    web2.prod.internal
    web3.prod.internal

hosts db [user=postgres, port=5433]:
    db-primary.prod.internal
    db-replica.prod.internal

# --- Reusable Prompt Templates --------------------------------------------

fn security_review:
    Review the code for security vulnerabilities.
    Check specifically for:
    - SQL injection
    - XSS (cross-site scripting)
    - Command injection
    - Path traversal
    Be concise and actionable.

fn code_quality:
    Check for code quality issues:
    - Functions longer than 50 lines
    - Duplicated logic
    - Missing error handling
    - Poor naming

# --- Tasks ----------------------------------------------------------------

# Build: shell prep + LLM reasoning (default task)
task build:
    !mkdir -p dist
    check if src/ has changed since the last build.
    if so, compile the TypeScript and bundle with esbuild.
    tag the output as {{project}}:latest.

# Test: run tests, then ask LLM to analyze any failures
task test: build:
    !@silent npm install
    !npm test
    if any tests failed, explain the root cause and suggest a fix.

# Security review with a thorough model
task review [llm=claude, model=opus, temperature=0.1]:
    @use security_review
    Focus on the git diff for {{project}}.
    !git diff --stat

# Fast lint with a cheaper model
task lint [llm=openai]:
    @use code_quality
    Apply to the src/ directory of {{project}}.

# Deploy with arguments; runs shell steps on web hosts via SSH
task deploy(target, port="8080"): build test [llm=openai, on=web]:
    !systemctl restart {{project}}
    verify {{project}} is running on {{target}} port {{port}}.
    @use security_review

# Database backup on db hosts
task backup [on=db]:
    !pg_dump {{project}} > /tmp/{{project}}-backup.sql
    !@silent gzip /tmp/{{project}}-backup.sql
    verify the backup completed successfully.

# Cleanup with silent + ignore
task clean:
    !@silent rm -rf dist/
    !@silent @ignore docker rmi {{project}}:latest
    cleaned up build artifacts for {{project}}.

# --- Docker ---------------------------------------------------------------

docker api-server [tag=latest]:
    A Python 3.11 slim image.
    Install requirements.txt with pip, no cache.
    Copy the app/ directory to /app.
    Set the working directory to /app.
    Expose port 8080.
    Run with gunicorn, 4 workers, binding 0.0.0.0:8080.
```

Run it:

```bash
# Build and test (default)
promptfile

# Deploy to staging on port 8080
promptfile deploy staging

# Deploy to production on port 443
promptfile deploy prod 443

# Security review with dry run
promptfile --dry-run review

# Build the Docker image
promptfile api-server

# Backup databases
promptfile backup

# Override env from CLI
promptfile deploy staging -V env=production

# List everything
promptfile --list
```

---

## File Format at a Glance

```
# Variables
name := "value"
backtick_var := `command`
concat := "a" + name + "b"
conditional := if name == "value" { "yes" } else { "no" }
export secret := "key"

# Set directives (Justfile-compatible)
set dotenv-load
set shell "bash"
set working-dir "/path"
set export
set positional-arguments
set fallback
set ignore-comments
set quiet

# LLM providers
llm <name> [model=..., key=$..., base_url=..., template=...]

# Host groups
hosts <name> [user=..., port=...]:
    hostname1
    hostname2

# Functions
fn <name>:
    reusable prompt text

# Tasks
task <name>[(arg1, arg2="default", +variadic)]: [dep1 dep2] [options]:
    !shell command
    !@silent command            # suppress output
    !@ignore command            # continue on failure
    !@command                   # quiet (suppress echoing)
    natural language prompt
    @use function_name
    running on {{os()}} / {{arch()}}
    {{if var == "val" { "yes" } else { "no" }}}

# Docker
docker <name> [tag=..., context=..., file=...]:
    description of the image in prose

# Includes
include "path/to/file.pf"

# Aliases
alias <short> := <task>

# Line continuation
long_line := "this is a very long" + \
    " value that spans lines"
```

---

## Contributing

Contributions are welcome. The project uses pytest for testing:

```bash
pip install -e .
pytest
```

---

## License

MIT
