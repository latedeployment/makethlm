# Settings & Directives

## Set Directives

Global configuration directives that affect the entire Promptfile:

```
set dotenv-load
set dotenv-load ".env.local"
set shell "bash"
set working-dir "/home/deploy/app"
```

### Directive Reference

| Directive | Description |
|-----------|-------------|
| `set dotenv-load` | Automatically load `.env` from the working directory |
| `set dotenv-load "path"` | Load a specific env file (enables loading and sets the path) |
| `set dotenv-path "path"` | Custom env file path (implicitly enables `dotenv-load`) |
| `set dotenv-required` | Error if the env file is missing |
| `set shell "name"` | Set the shell used for `!` commands (default: system shell) |
| `set working-dir "path"` | Set the working directory for all tasks |
| `set export` | Export all variables to the environment |
| `set positional-arguments` | Pass task arguments as `$1`, `$2`, etc. |
| `set ignore-comments` | Strip `#` comments from shell commands |
| `set quiet` | Suppress command echoing globally |
| `set tempdir "path"` | Temporary directory for recipes |
| `set allow-duplicate-tasks` | Allow redefining tasks (last wins) |
| `set allow-duplicate-variables` | Allow redefining variables |

### Variable References in Directives

String directive values support the same expressions as variable declarations: quoted strings, variable references via `+` concatenation, backtick commands, and if/else expressions. Variables declared above the directive are available:

```
project := "/opt/myapp"
env := "production"

set working-dir project + "/src"
set dotenv-load project + "/.env." + env
set tempdir project + "/tmp"
set shell if env == "production" { "/bin/bash" } else { "/bin/sh" }
```

### Environment Files (dotenv)

By default, environment files are not loaded. Use `set dotenv-load` to enable loading:

```
# Load the default .env file
set dotenv-load

# Load a specific file instead of .env
set dotenv-load ".env.local"
set dotenv-load ".env.production"
set dotenv-load "config/.env"

# Use variables declared above
config_dir := "/etc/myapp"
set dotenv-load config_dir + "/.env"

# At runtime, paths also support $ENV_VAR and ~ expansion
set dotenv-load "$HOME/.env"
set dotenv-load "~/.config/myapp/.env"
```

When a path is passed directly to `dotenv-load`, it both enables loading and sets the file path in a single directive. This is equivalent to writing both `set dotenv-load` and `set dotenv-path`:

```
# These two forms are equivalent:
set dotenv-load ".env.local"

set dotenv-load
set dotenv-path ".env.local"
```

Setting `dotenv-path` on its own implicitly enables `dotenv-load` — there is no need to specify both:

```
# dotenv-path implies dotenv-load
set dotenv-path ".env.staging"
```

Add `set dotenv-required` to raise an error if the env file is missing (by default a missing file is silently ignored):

```
set dotenv-load ".env.production"
set dotenv-required
```

## Includes

Split large Promptfiles across multiple files with the `include` directive:

```
include "common/variables.pf"
include "tasks/deploy.pf"
include "tasks/docker.pf"
```

Included files are resolved relative to the file containing the `include` statement. All definitions (variables, functions, tasks, LLM providers, host groups) from the included file are merged into the current file.

**Override precedence:** If both the included file and the including file define the same variable, function, or task, the **local definition wins**.

**Circular include detection:** If file A includes file B, and file B includes file A, the parser raises an error.

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

## Aliases

Create short aliases for tasks:

```
alias d := deploy
alias t := test
alias r := review
```

After defining an alias, `makethlm d` is equivalent to `makethlm deploy`.
