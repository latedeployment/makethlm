# Tasks

## Defining Tasks

A makethlm-native task is defined with the `task` keyword, a name, and a colon.
The indented body is a mix of LLM prompts and shell commands.

```
task build:
    !mkdir -p dist
    check if moo.md is newer than the Dockerfile.
    if so, rebuild the docker image from scratch.
    tag it as {{project}}:latest.
```

Bare Just-style recipes are supported for shell-only workflows:

```
build:
    cargo build

test: build
    cargo test
```

Bare recipe body lines are shell commands by default. Use `task` when you want
LLM prompt lines.

The **first task** defined in the file is the **default task**. Running `makethlm` with no arguments executes it.

Consecutive lines of natural language are merged into a single prompt and sent to the LLM together. Shell commands (`!` lines) break prompt boundaries, so prompts before and after a shell command become separate LLM calls. Shell output can be captured for later prompt steps with `!cmd -> name`, then referenced as `{{name.stdout}}`, or piped into the next prompt with `!cmd |>`.

## Dependencies

A task can depend on other tasks:

```
task deploy: build test:
    deploy to production
```

This means: run `build` first, then `test`, then `deploy`. Dependencies are resolved via topological sort, so transitive dependencies and diamond dependencies work correctly. Cycles are detected and reported as errors.

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

Running `makethlm d` executes: `a`, then `b` and `c` (in dependency order), then `d`.

## Task Arguments

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

Arguments are interpolated via `{{name}}` just like variables. If a required argument is not provided, makethlm exits with an error.

Arguments are **scoped to the target task** -- they are not passed to dependency tasks.

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

## Task Options

Tasks accept metadata options in square brackets:

```
task review [llm=claude, model=opus, temperature=0.2, max_tokens=4096]:
    review the code carefully
```

Options can be combined with arguments and dependencies:

```
task deploy(target) [llm=openai, on=web, model=gpt-4]: build test
    deploy {{project}} to {{target}}
```

### Full Option Reference

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
| `confirm` | flag/string | Prompt for confirmation before running. Use `confirm` for a default message or `confirm="Are you sure?"` for a custom one |
| `os` | string | Only run this task on the specified OS (e.g., `os=linux`) |
| `linux` | flag | Shorthand for `os=linux` |
| `macos` | flag | Shorthand for `os=macos` |
| `windows` | flag | Shorthand for `os=windows` |
| `unix` | flag | Shorthand for `os=unix` (matches Linux and macOS) |
| `working-dir` | string | Change to this directory before executing the task |
| `no-cd` | flag | Don't change to working directory (overrides `set working-dir`) |
| `no-exit-message` | flag | Suppress error message on failure |
| `no-quiet` | flag | Override global `set quiet` for this task |
| `positional-arguments` | flag | Per-task override for positional argument passing |
| `timeout` | duration | Shell and SSH command timeout, e.g. `timeout=30s` or `timeout=5m` |
| `llm-timeout` | duration | Prompt/LLM timeout, e.g. `llm-timeout=10m` |
| `rollback` | string | Task to run if this task fails |
| `ssh-key` | string | SSH identity file for this task's remote shell steps |
| `ssh-strict-host-key-checking` | string | SSH host key policy: `yes`, `no`, or `accept-new` |
| `ssh-parallel` | flag | Run each shell step across all target hosts concurrently |
