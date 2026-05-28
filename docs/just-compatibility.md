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

- `mod` submodules and `module::recipe` invocation.
- Just's full attribute syntax, including `[script]`, `[extension]`,
  `[metadata]`, and `[env]`.
- Shebang/script recipes.
- Subsequent dependencies with `&&`.
- Multiple recipe invocation in one command.
- Fallback/global justfile search.
- Shell setting array syntax, such as `set shell := ["bash", "-cu"]`.
- More built-in functions, especially path and environment helpers.

## Implementation Priority

1. Add shebang/script recipe support.
2. Add `mod` parsing and `module::recipe` execution.
3. Add richer `--list` output.
