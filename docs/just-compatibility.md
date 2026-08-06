# Just Compatibility

makethlm intentionally borrows from `just`, but it is not a full `just`
implementation. The goal is to support the common authoring patterns while
keeping makethlm's LLM-oriented task model.

## Implemented

| Just feature | makethlm status |
|--------------|-----------------|
| Bare recipes | Supported as shell-only tasks: `build:` |
| Dependencies | Supported: `test: build` and `task test: build:` |
| Recipe arguments | Supported: `deploy target port="8080":` and `task deploy(target, port="8080"):` |
| Variadic arguments | Supported with `+args` and `*args` |
| Aliases | Supported with `alias b := build` |
| Private recipes | Supported with `_name` and `[private]` |
| Quiet recipe lines | Supported in bare recipes with `@command`; in `task` bodies use `!@quiet command` |
| Ignored shell errors | Supported in bare recipes with `-command`; in `task` bodies use `!@ignore command` |
| OS attributes | Supported: `[linux]`, `[macos]`, `[windows]`, `[unix]` |
| Working directory controls | Supported: `set working-dir`, `[working-dir=...]`, `[no-cd]` |
| Dotenv loading | Supported with `set dotenv-load`, `set dotenv-path`, and `set dotenv-required` |
| Duplicate override settings | Supported with `set allow-duplicate-tasks` and `set allow-duplicate-variables` |
| Includes/imports | Supported with `include "path"`, `import "path"`, and optional `import? "path"` |
| Default attribute | Supported with `[default]` |
| Confirmation attributes | Supported with `[confirm]`, `[confirm="message"]`, and `[confirm("message")]` |
| Env attributes | Supported with `[env(NAME, VALUE)]` for task shell steps |
| Script recipes | Supported with shebang recipes, `[script]`, and `[script("COMMAND")]` |
| Subsequent dependencies | Supported with `&&` |
| Module recipes | Supported with `mod name "path"` and `name::recipe` task names; variables, functions, providers, agents, hosts, guidance, aliases, and hooks stay namespaced |
| Multiple recipe invocation | Supported with `makethlm task1 task2` when tasks have no args |
| Shell array setting | Supported with `set shell := ["bash", "-cu"]` |
| Fallback/global search | Searches parent directories and `$XDG_CONFIG_HOME/makethlm/Promptfile` |
| Listing and summaries | Supported with `--list` and `--summary` |
| Shell completions | Supported with `makethlm completions bash|zsh|fish` |

## Deliberate Differences

Bare recipes are treated as Just-style shell tasks. `task` definitions are
makethlm-native and may freely interleave shell steps (`!command`) with LLM
prompts.

```make
build:
    cargo build

task review:
    !git diff --name-only |>
    review the changed files for security issues
```

`-> name` shell-step capture and `|>` shell-to-prompt piping are makethlm
extensions, not Just syntax.

## Missing or Partial

These are the largest gaps to close for stronger Just compatibility:

- Unqualified module imports and the remaining edge cases of full Just module parity.
- Full Just attribute parity for `[metadata]` output shape and advanced `[env]`
  behavior.
- More built-in functions, especially edge-case path helpers.

## Implementation Priority

1. Add richer `--list` output.
2. Expand module compatibility.
3. Expand built-in function parity.
