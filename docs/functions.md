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
