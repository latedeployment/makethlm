# makethlm

**A task runner where tasks are LLM prompts.**

[![PyPI version](https://img.shields.io/pypi/v/makethlm)](https://pypi.org/project/makethlm/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-latedeployment.github.io-blue)](https://latedeployment.github.io/makethlm/)

makethlm is a command-line task runner in the tradition of [Make](https://www.gnu.org/software/make/) and [Just](https://github.com/casey/just), but
designed for a world where tasks are described in natural language and executed
by LLMs. Define your build, deploy, review, and maintenance workflows as
prose, interleave them with shell commands, and let your LLM of choice do the
heavy lifting.

Recent additions include failure postmortems, typed artifact contracts,
provider retries and fallbacks, capability inspection, reproducible caching,
redacted run replay, stricter safety controls, and deployment-ready examples.

```
# Promptfile

project := "my-web-app"

llm claude [model=opus]

task build [sources="src/**/*.ts", outputs="dist/bundle.js"]:
    !mkdir -p dist
    compile the TypeScript in src/ and bundle it to dist/bundle.js with esbuild.

task test: build:
    !npm test
    if any tests failed, explain the root cause and suggest/apply a fix.

task deploy(target, port="8080"): build test:
    !systemctl restart {{project}}
    verify {{project}} is running on {{target}} port {{port}}.
```

```
$ makethlm deploy staging
[ok] build
  Bundled 14 TypeScript files into dist/bundle.js.
[ok] test
  All 32 tests passed.
[ok] deploy
  Verified my-web-app is running on staging port 8080. All health checks pass.
```

Run it again without touching `src/` and the build is skipped, because
`sources` and `outputs` give the task the same file-dependency tracking `make`
has — no LLM call needed to work out that nothing changed:

```
$ makethlm deploy staging
[ok] build
  [skipped] up to date (14 sources older than outputs)
[ok] test
  All 32 tests passed.
[ok] deploy
  Verified my-web-app is running on staging port 8080. All health checks pass.
```

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
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
  - [Reliable Workflows](#reliable-workflows)
  - [Modules](#modules)
  - [Set Directives](#set-directives)
  - [Aliases](#aliases)
- [Safety and Capability Inspection](#safety-and-capability-inspection)
- [CLI Reference](#cli-reference)
- [Comparison with Make and Just](#comparison-with-make-and-just)

---

## Installation

```bash
pip install makethlm
```

Requires Python 3.10 or newer.

By default, makethlm uses the [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) as its LLM backend. Make sure it is installed and authenticated:

```bash
claude --version
```

To use a different provider, see [LLM Provider Selection](#llm-provider-selection).

---

## Quick Start

Simple a C project where the LLM generates a library and shell commands compile it:

```
# Promptfile

project := "hello"

llm claude [model=sonnet]

task generate-lib:
    !mkdir -p src
    Write a small C library with a header file.
    The library should provide two functions:
      - char *greet(const char *name) that returns "Hello, <name>!"
      - int add(int a, int b) that returns the sum
    Output ONLY the contents of two files, clearly separated:
    First src/mylib.h (the header), then src/mylib.c (the implementation).
    Use standard C, no external dependencies.
    Generate src/main.c for the library as well

task build:
    !gcc -c src/mylib.c -o src/mylib.o
    !gcc src/main.c src/mylib.o -o {{project}}
    !echo "Build complete: ./{{project}}"

task run: build:
    !./{{project}}

task clean:
    !rm -f src/*.o {{project}}
    !echo "Cleaned."
```

```bash
makethlm generate-lib   # LLM writes the C code
makethlm run            # compiles (via build dependency) and runs
makethlm --dry-run run  # preview without executing
makethlm --list         # see all tasks
```

Lines starting with `!` are shell commands. Everything else is a prompt sent to the LLM. Tasks can depend on each other (`run: build` means "run build first").

Full documentation: **<https://latedeployment.github.io/makethlm/>**

More examples in [`examples/`](examples/).
See also [`examples/cmake-project/`](examples/cmake-project/),
[`examples/compiler-diagnostics/`](examples/compiler-diagnostics/), and
[`examples/python-ci/`](examples/python-ci/).


## Promptfile Syntax Reference

makethlm looks for a file named `Promptfile`, `promptfile`, `Promptfile.pf`, `promptfile.pf`, `.promptfile`,
`.Promptfile`, `.promptfile.pf`, `.Promptfile.pf`, `PROMPTFILE`, or `PROMPTFILE.pf` in the current directory,
then in each parent directory, then at
`$XDG_CONFIG_HOME/makethlm/Promptfile`. `-f` overrides discovery.

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
makethlm deploy -V env=production
```

### Tasks

A makethlm-native task is defined with the `task` keyword, a name, and a
colon. The indented body that follows is a mix of LLM prompts (natural
language) and shell commands.

```
task build:
    !mkdir -p dist
    check if moo.md is newer than the Dockerfile.
    if so, rebuild the docker image from scratch.
    tag it as {{project}}:latest.
```

Bare Just-style recipes are also supported for shell-only workflows:

```
build:
    cargo build

test: build
    cargo test
```

Bare recipe body lines are shell commands by default. Use `task` when you want
LLM prompt lines.

The **first task** defined in the file is the **default task**. Running
`makethlm` with no arguments executes it.

Consecutive lines of natural language are merged into a single prompt and sent
to the LLM together. Shell commands (lines starting with `!`) break prompt
boundaries, so LLM prompts before and after a shell command become separate LLM
calls.

### Dependencies

A task can depend on other tasks. Dependencies are listed after the colon:

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

Running `makethlm d` executes: `a`, then `b` and `c` (in dependency order),
then `d`.

### Task Arguments

Tasks can accept positional arguments with optional defaults:

```
task deploy(target, port="8080"):
    deploy {{project}} to {{target}} on port {{port}}
```

Pass arguments on the command line after the task name:

```bash
makethlm deploy staging        # target=staging, port=8080 (default)
makethlm deploy prod 443       # target=prod, port=443
```

Arguments are interpolated into both prompt text and shell commands via the
same `{{name}}` syntax as variables. If a required argument (one without a
default) is not provided, makethlm exits with an error.

Interpolation does not shell-escape values. In shell steps, quote variable and
argument values explicitly with `{{quote(name)}}`.

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
This is one of makethlm's defining features -- run a command, reason about
its output, run another command:

```
task analyze:
    !git diff --name-only -> changed
    review these changed files for security issues:
    {{changed.stdout}}

    !npm test 2>&1 || true -> tests
    if tests failed, explain the root cause:
    {{tests.stdout}}
```

Use `|>` to pass a command's output directly into the next prompt:

```
task review:
    !git diff --name-only |>
    review the changed files for security issues
```

Variable interpolation (`{{name}}`) works inside shell commands:

```
project := "myapp"

task build:
    !docker build -t {{quote(project)}}:latest .
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
LLM generates a Dockerfile, and makethlm builds it automatically.

```
docker api-server [tag=latest]:
    A Python 3.11 slim image.
    Install requirements.txt with pip, no cache.
    Copy the app/ directory to /app.
    Set the working directory to /app.
    Expose port 8080.
    Run with gunicorn, 4 workers, binding 0.0.0.0:8080.
```

Running `makethlm api-server` will:

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

makethlm supports multiple LLM providers. Declare them globally and
optionally override per task.

```
llm claude [model=opus]
llm codex [model=gpt-5-codex]
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

**Codex CLI** can be used as a first-class provider:

```
llm codex [model=gpt-5-codex]

task review [llm=codex]:
    review the current diff and suggest fixes
```

makethlm invokes `codex exec` non-interactively. Install and authenticate
Codex first with `codex login`.

**Native OpenAI and Ollama providers** do not require shell templates:

```
llm openai [model=gpt-4o-mini, key=$OPENAI_API_KEY]
llm ollama [model=llama3, base_url=http://127.0.0.1:11434]
```

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
makethlm review --model sonnet
```

Or bypass all configured providers and use an arbitrary CLI:

```bash
makethlm review --shell 'ollama run llama3 "{prompt}"'
```

### Host Inventory (SSH)

makethlm includes an Ansible-like host inventory for running shell commands
on remote machines via SSH.

**Define host groups:**

```
hosts web [user=deploy, port=22, identity-file=~/.ssh/deploy, strict-host-key-checking=accept-new]:
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
  SSH, sequentially by default. If any host fails, execution stops.
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
| `identity-file` | (none -- uses SSH default) | SSH identity file |
| `strict-host-key-checking` | (none -- uses SSH default) | SSH host key policy: `yes`, `no`, or `accept-new` |

SSH connections use `BatchMode=yes` for non-interactive operation.

Ansible INI inventories are also supported. Per-host `ansible_user` and
`ansible_port` values remain attached to the host that declared them rather
than leaking into group defaults. See
[SSH and host inventory](docs/ssh.md) for an example.

Task-level SSH options can override host group settings:

```
task deploy [on=web, ssh-key=~/.ssh/deploy, ssh-strict-host-key-checking=yes, ssh-parallel, timeout=45s]:
    !systemctl restart myapp
```

`ssh-parallel` runs each shell step across all hosts concurrently, then waits
for every host before moving to the next step.

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
| `max_tokens` | int | Maximum tokens in the LLM response, from 1 through 1,000,000 |
| `llm` | string | Provider name; `"a\|b"` fans out to several at once |
| `judge` | string | Provider that merges fan-out answers into one response |
| `mcp` | string | MCP servers to attach, e.g. `mcp="files\|github"` |
| `agent` | string | Named agent whose instructions/provider apply to this task |
| `on` | string | Host group to execute shell commands on via SSH |
| `private` | flag | Hide this task from `--list` output (also: `_`-prefixed tasks) |
| `group` | string | Group heading for `--list` (e.g., `group="deploy"`) |
| `doc` | string | Description shown in `--list` output |
| `confirm` | flag/string | Prompt for confirmation before running. Use `confirm` for the default message or `confirm="Are you sure?"` for a custom one |
| `default` | flag | Make this task the default target |
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
| `register` | string | Store the task result under a custom artifact name |
| `webhook` | string | Deliver the task result to an HTTP(S) webhook |
| `webhook-on` | string | Deliver on `always`, `success`, or `failure` |
| `secrets` | string | Override the configured secret backend for this task |
| `when` | expression | Run only when the condition is true; repeat for multiple conditions |
| `cache` | duration | Reuse successful results for a duration such as `30m`, `1h`, or `1d` |
| `sources` | string | Input file patterns; skip the task when outputs are newer |
| `outputs` | string | Output file patterns compared against `sources` |
| `timeout` | duration | Shell and SSH command timeout, e.g. `timeout=30s` or `timeout=5m` |
| `llm-timeout` | duration | Prompt/LLM timeout, e.g. `llm-timeout=10m` |
| `rollback` | string | Task to run if this task fails |
| `postmortem` | string | Diagnostic task to run after failure and before rollback |
| `fallback-llm` | string | Up to four `|`-separated providers to try after the primary provider |
| `retries` | integer | LLM retries per provider, from 0 through 10 |
| `requires` | string | `|`-separated `artifact.field[:type]` input contracts |
| `produces` | string | Output contract: `text`, `nonempty`, `json`, `object`, `array`, `integer`, `number`, or `boolean` |
| `repair` | int | Re-prompt up to N times (max 3) when `produces` is violated |
| `max-cost` | string | Stop the run when LLM spend reaches this many US dollars |
| `ssh-key` | string | SSH identity file for this task's remote shell steps |
| `ssh-strict-host-key-checking` | string | SSH host key policy: `yes`, `no`, or `accept-new` |
| `ssh-parallel` | flag | Run each shell step across all target hosts concurrently |
| `sandbox` | string | Wrap shell steps with `docker`, `systemd`, `bwrap`, or `none` |
| `sandbox-image` | string | Docker sandbox image, defaulting to `ubuntu:latest` |
| `sandbox-mount` | string | Extra Docker mount in `source:target[:mode]` form |
| `sandbox-net` | string | Sandbox network mode: `none` (default) or `host` |
| `sandbox-read-only` | flag | Mount the workspace read-only where supported |
| `script` | flag/string | Run the task body as one temporary script; use `script("python3")` to choose an interpreter |
| `extension("ext")` | string | File extension for script recipes, e.g. `extension("py")` |
| `metadata` | flag | Mark the task for Just-compatible metadata output |
| `env(NAME, VALUE)` | string | Set an environment variable for task shell steps |

Options can be combined with dependencies:

```
task deploy(target) [llm=openai, on=web, model=gpt-4]: build test:
    deploy {{project}} to {{target}}
```

### Reliable Workflows

Postmortems run after a failed task and before rollback. They can inspect the
failed task's redacted artifact:

```
task diagnose:
    explain deploy exit {{deploy.exit_code}}:
    {{deploy.stdout}}

task restore:
    !./scripts/restore

task deploy [postmortem=diagnose, rollback=restore]:
    !./scripts/deploy
```

Artifact contracts validate task boundaries:

```
task inspect [produces=object]:
    !./scripts/inspect --json

task publish [requires="inspect.stdout:object"]: inspect:
    !./scripts/publish
```

Provider retry and fallback strategies are declared per task:

```
llm cloud [template=cloud-llm {prompt}]
llm local [model=llama3]

task review [llm=cloud, retries=1, fallback-llm=local]:
    review the release diff
```

Skip a task whose outputs are already newer than its inputs, the way `make`
does:

```
task build [sources="src/*.c, include/*.h", outputs="build/app"]:
    !mkdir -p build
    !cc -o build/app src/*.c
```

Patterns are comma-separated and support `*`, `?`, `[...]`, and recursive `**`.
The task runs whenever an output is missing, no source matched, or any source
is newer than the oldest output. `--always-make` (`-B`) forces a run.

Enable caching with a duration:

```
task inspect [cache=1h, produces=object]:
    !./scripts/inspect --json
```

Cache keys include resolved execution inputs such as `sources` file contents,
arguments, variables,
expanded `@use` functions, upstream artifacts, provider and agent
configuration, task options, and referenced environment variables. Failed
tasks and tasks that read sensitive inputs are not cached. Cached step results
are restored so downstream artifacts behave the same as a fresh run.

Successful and failed non-dry runs are stored as redacted local bundles:

```bash
makethlm history
makethlm replay 42
makethlm --json replay 42
```

Replay only displays the recorded run; it never executes the task again. See
[Reliable Workflows](docs/reliability.md) for the full behavior of
postmortems, contracts, retries, caching, and replay.

### Modules

Use modules to reuse a Promptfile without merging its names into the parent:

```make
# Promptfile
mod ops "ops.pf"

task release: ops::deploy:
    @echo release complete
```

Tasks are invoked as `ops::deploy`. Variables, functions, providers, agents,
host groups, guidance, aliases, rollback hooks, postmortems, and nested modules
remain under the same `ops::` namespace. `makethlm --list` shows module tasks
and aliases explicitly.

### Set Directives

Global configuration directives that affect the entire Promptfile:

```
set dotenv-load
set dotenv-load ".env.local"
set secrets "env"
set shell "bash"
set working-dir "/home/deploy/app"
```

| Directive | Description |
|-----------|-------------|
| `set dotenv-load` | Automatically load `.env` from the working directory |
| `set dotenv-load "path"` | Load a specific env file (enables loading and sets the path) |
| `set dotenv-path "path"` | Custom env file path (implicitly enables `dotenv-load`) |
| `set dotenv-required` | Error if the env file is missing |
| `set secrets "backend"` | Select a secrets backend such as `env`, `infisical`, `1password`, or `sops` |
| `set secrets-project "name"` | Infisical project name or ID |
| `set secrets-environment "name"` | Infisical environment name |
| `set secrets-vault "name"` | 1Password vault name |
| `set secrets-file "path"` | SOPS encrypted secrets file |
| `set shell "name"` | Set the shell used for `!` commands (default: system shell) |
| `set working-dir "path"` | Set the working directory for all tasks |
| `set export` | Export all variables to the environment |
| `set positional-arguments` | Pass task arguments as `$1`, `$2`, etc. |
| `set ignore-comments` | Strip `#` comments from shell commands |
| `set quiet` | Suppress command echoing globally |
| `set tempdir "path"` | Temporary directory for recipes |
| `set allow-duplicate-tasks` | Allow redefining tasks (last wins) |
| `set allow-duplicate-variables` | Allow redefining variables |

`dotenv-load` accepts an optional file path. When a path is provided, it both enables loading and sets the file — equivalent to `set dotenv-load` plus `set dotenv-path`. Setting `dotenv-path` alone also implicitly enables loading. At runtime, dotenv paths also support `$ENV_VAR` and `~` expansion.

Secrets can be injected with `{{#secret:NAME}}`. The placeholder is resolved at
runtime and masked in dry-run and plan output:

```make
set secrets "env"

task deploy:
    !curl -H "Authorization: Bearer {{#secret:DEPLOY_TOKEN}}" https://api.example.com/deploy
```

String directive values support the same expressions as variable declarations — concatenation with `+`, backtick commands, and if/else:

```
project := "/opt/myapp"
set working-dir project + "/src"
set dotenv-load project + "/.env.production"
```

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

### Automatic Variables

Every task can reference its own target, dependencies, and files without
repeating them, the way make's automatic variables work:

| Variable | Make equivalent | Value |
|----------|-----------------|-------|
| `{{makethlm_task}}` | `$@` | Name of the running task |
| `{{makethlm_deps}}` | `$^` | All dependency task names, space-separated |
| `{{makethlm_dep}}` | `$<` | First dependency task name |
| `{{makethlm_sources}}` | — | Files matched by `sources`, relative to the working directory |
| `{{makethlm_outputs}}` | — | Files matched by `outputs`, falling back to the declared patterns |
| `{{makethlm_changed}}` | `$?` | Only the sources newer than the oldest output |
| `{{makethlm_file}}` | — | Path to the Promptfile |
| `{{makethlm_dir}}` | — | Directory containing the Promptfile |

```
task build [sources="src/*.c", outputs="build/app"]:
    !mkdir -p build
    !cc -o {{makethlm_outputs}} {{makethlm_sources}}
```

`makethlm_changed` is the incremental lever — it holds only the sources newer
than the output, so a recipe can act on what actually changed:

```
task lint [sources="src/**/*.py", outputs=".lint-stamp"]:
    !ruff check {{makethlm_changed}}
    !touch .lint-stamp
```

Paths are rendered relative to the directory the recipe runs in and quoted only
when they need it, so names containing spaces stay safe. The file variables are
empty for a task that declares neither `sources` nor `outputs`.

### How they expand

The variables are plain text substitution, like make's — the value is spliced
into the command before the shell sees it. Multiple paths are **separated by a
single space**, never a comma or semicolon, and each path is quoted only when it
contains something the shell would otherwise split on:

```
!cc -o {{makethlm_outputs}} {{makethlm_sources}}
# the shell receives:
cc -o build/app src/alpha.c src/beta.c 'src/two words.c'
```

Because the quoting is per path, ordinary word splitting iterates them
correctly, including names with spaces:

```
task check [sources="src/*.c", outputs="build/app"]:
    !for f in {{makethlm_sources}}; do echo "checking $f"; done
    !set -- {{makethlm_sources}}; echo "$# files"
```

In a `script` recipe they can fill a bash array, which is sturdier for anything
non-trivial:

```
task report [script("bash"), sources="src/*.c", outputs="build/app"]:
    files=({{makethlm_sources}})
    echo "${#files[@]} sources"
    for f in "${files[@]}"; do
        printf '%s: %s bytes\n' "$f" "$(wc -c < "$f")"
    done
```

One gotcha, shared with make: wrapping the variable in double quotes collapses
it into a single argument, and the per-path quotes become literal characters.

```
!for f in "{{makethlm_sources}}"; do ...   # one iteration, not three
!for f in {{makethlm_sources}}; do ...     # correct
```

Leave them unquoted and let the per-path quoting do its job.

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

### String Functions

String manipulation functions can be used inside `{{ }}` templates on variables:

**String manipulation:**

| Function | Example | Result |
|----------|---------|--------|
| `uppercase(s)` | `{{uppercase(name)}}` | `HELLO` |
| `lowercase(s)` | `{{lowercase(name)}}` | `hello` |
| `trim(s)` | `{{trim(padded)}}` | `hello` |
| `trim_start(s)` | `{{trim_start(padded)}}` | `hello ` |
| `trim_end(s)` | `{{trim_end(padded)}}` | ` hello` |
| `replace(s, from, to)` | `{{replace(path, "/", "-")}}` | `src-main` |
| `replace_regex(s, pat, to)` | `{{replace_regex(ver, "\\d+$", "0")}}` | `1.2.0` |
| `quote(s)` | `{{quote(cmd)}}` | `'hello world'` |
| `join(sep, a, b, ...)` | `{{join(", ", "a", "b")}}` | `a, b` |
| `env_var(name[, default])` | `{{env_var("HOME")}}` | `/home/user` |
| `path_exists(p)` | `{{path_exists("README.md")}}` | `true` |
| `len(s)` | `{{len(name)}}` | `5` |
| `substr(s, start[, len])` | `{{substr(name, 0, 3)}}` | `hel` |
| `match(s, regex)` | `{{match(ver, "^\\d+")}}` | `true` |

**Path functions:**

| Function | Example | Result |
|----------|---------|--------|
| `file_name(p)` | `{{file_name("/tmp/a.txt")}}` | `a.txt` |
| `file_stem(p)` | `{{file_stem("/tmp/a.txt")}}` | `a` |
| `extension(p)` | `{{extension("a.tar.gz")}}` | `gz` |
| `without_extension(p)` | `{{without_extension("a.tar.gz")}}` | `a.tar` |
| `parent_directory(p)` | `{{parent_directory("/tmp/a.txt")}}` | `/tmp` |

**Boolean functions** (return `"true"` or `"false"`):

| Function | Description |
|----------|-------------|
| `contains(s, sub)` | Whether `s` contains `sub` |
| `starts_with(s, prefix)` | Whether `s` starts with `prefix` |
| `ends_with(s, suffix)` | Whether `s` ends with `suffix` |

**Version functions:**

| Function | Example | Result |
|----------|---------|--------|
| `version_major(v)` | `{{version_major("1.2.3")}}` | `1` |
| `version_minor(v)` | `{{version_minor("1.2.3")}}` | `2` |
| `version_patch(v)` | `{{version_patch("1.2.3")}}` | `3` |
| `bump_major(v)` | `{{bump_major("1.2.3")}}` | `2.0.0` |
| `bump_minor(v)` | `{{bump_minor("1.2.3")}}` | `1.3.0` |
| `bump_patch(v)` | `{{bump_patch("1.2.3")}}` | `1.2.4` |

```
version := "1.2.3"

task release:
    @echo "Current: {{version}}, next: {{bump_minor(version)}}"
    !git tag v{{bump_minor(version)}}
    deploy version {{uppercase(version)}} to production
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
makethlm deploy staging prod     # targets="staging prod"
makethlm greet alice bob         # names="alice bob"
makethlm greet                   # names="" (ok for *)
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

After defining an alias, `makethlm d` is equivalent to `makethlm deploy`.

---

## Safety and Capability Inspection

Before running an unfamiliar task, inspect its complete dependency,
postmortem, and rollback closure:

```bash
makethlm --capabilities deploy
makethlm --capabilities --json deploy
makethlm --plan deploy
```

`--safe` blocks capabilities unless their corresponding flags are passed.
Shell, SSH, Docker, LLM, secret/environment reads, and webhooks have separate
permissions. Claude CLI, Codex CLI, and shell-template providers can execute
locally, so they require both `--allow-llm` and `--allow-shell`; native OpenAI
and Ollama HTTP providers do not add the local-shell capability.

Inspection commands disable parse-time backticks unless
`--allow-backticks` is explicitly supplied. Plans, output, history, replay
bundles, artifacts, and webhook bodies redact known secret values. See
[Security](docs/security.md) for the full threat model and redaction rules.

---

## CLI Reference

```
makethlm [OPTIONS] [TASK] [ARGS...]
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
| `--check` | | Validate Promptfile references, required tools, and risky capabilities |
| `--capabilities` | | Explain transitive execution capabilities and their safe-mode flags |
| `--plan` | | Preview execution order, variables, providers, hosts, and resolved steps |
| `--graph` | | Print a task dependency graph and exit |
| `--graph-format FORMAT` | | Graph format: `mermaid` or `dot` |
| `--history [N]` | | Show recent local run history and exit |
| `--no-history` | | Do not record this run in local history |
| `--evaluate EXPR` | | Evaluate an expression and print the result |
| `--dry-run` | | Print prompts and commands without executing them |
| `--json` | | Emit machine-readable JSON output |
| `--parallel` | | Run independent dependency tasks in parallel |
| `--always-make` | `-B` | Run tasks even when sources are unchanged or results are cached |
| `--since REF` | | Git ref that `changed()`/`changed_files()` compare against |
| `--watch` | | Re-run the target whenever a watched source file changes |
| `--watch-interval SECONDS` | | Polling interval for `--watch` (default: 1.0) |
| `--max-cost USD` | | Stop the run once LLM spend reaches this many US dollars |
| `--log-llm PATH` | | Append every LLM call to PATH as JSONL for live debugging |
| `--fixtures DIR` | | Serve LLM responses from recorded fixtures in DIR |
| `--record-fixtures` | | Call providers normally and record responses into `--fixtures DIR` |
| `--jobs N` | | Limit parallel task workers; implies `--parallel` |
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

**Examples:**

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

# Format Promptfiles
makethlm fmt
makethlm fmt --check

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

# Record LLM responses once, then re-run offline in CI
makethlm --fixtures tests/fixtures --record-fixtures review
makethlm --fixtures tests/fixtures review

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

# List everything
makethlm --list
```

Parallel execution runs tasks at the same dependency depth concurrently. If any
task in a level fails, later levels are skipped and the run fails.

**`--list` output** includes tasks (with dependencies, arguments, options),
functions, LLM providers (with the default marked), and host groups:

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

---

## Comparison with Make and Just

| Feature | Make | Just | makethlm |
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
| Fallback search | N/A | `set fallback` | N/A |
| File composition | `include` | `import` | `include` |
| Dry run | `-n` flag | `--dry-run` | `--dry-run` |
| Dump structure | N/A | `--dump` | `--dump` |
| File name | `Makefile` | `justfile` | `Promptfile` |

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
set dotenv-load ".env.local"
set shell "bash"
set working-dir "/path"
set export
set positional-arguments
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
task <name>[(arg1, arg2="default", +variadic)] [options]: [dep1 dep2]:
    !shell command
    !shell command -> captured   # later: {{captured.stdout}}
    !shell command |>            # pipe output into next prompt
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
import "path/to/file.pf"
import? "optional-file.pf"

# Aliases
alias <short> := <task>

# Line continuation
long_line := "this is a very long" + \
    " value that spans lines"
```

---

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ -q --no-docker
./publish.sh --validate --skip-tests
```

Use these checks before committing changes that touch parser, runner, CLI, or
packaging behavior.

Release preparation is scripted:

```bash
scripts/release.py patch        # bump, test, validate, commit, tag
scripts/release.py minor --publish
```

---

## License

MIT
