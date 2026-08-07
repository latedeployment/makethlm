# Functions

## Defining Functions

Functions are reusable prompt templates defined with `fn`. They are injected into task bodies with the `@use` directive.

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
```

## Using Functions

Reference functions in tasks with `@use`:

```
task review:
    @use security_review
    Focus on the git diff for the current PR.

task full-review:
    @use security_review
    @use code_quality
    Apply to the entire src/ directory.
```

When a task is executed, every `@use name` line is replaced with the full text of the named function. Multiple `@use` directives can appear in the same task.

!!! note
    Functions cannot themselves contain `@use` (no recursive expansion).

## Git-Aware Inputs

These scope a task to what actually changed, which is what makes an LLM review
task usable in CI:

| Function | Returns |
|----------|---------|
| `changed("PATTERN")` | `true` when any changed path matches the glob |
| `changed("REF", "PATTERN")` | the same, compared against an explicit ref |
| `changed_files()` | space-separated changed paths |
| `changed_files("REF")` | the same, compared against an explicit ref |
| `git_branch()` | current branch name |
| `git_sha()` | short commit SHA; `git_sha("false")` for the full SHA |

"Changed" means differing from the ref plus untracked, non-ignored files. The
default ref is `HEAD`; `--since REF` changes it for a whole run.

```make
task review [when=changed("src/**") == "true"]:
    review only these files: {{changed_files()}}
```

```bash
makethlm --since main review
```

Git commands are read-only and run with fixed arguments, never through a shell.
Outside a repository, or without git installed, `changed_files()` is empty,
`changed()` is `false`, and `git_branch()`/`git_sha()` are empty strings, so a
Promptfile still works in a plain directory.
