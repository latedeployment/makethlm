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
