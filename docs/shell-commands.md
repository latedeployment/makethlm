# Shell Commands

## Basics

Lines starting with `!` are shell commands executed as subprocesses:

```
task setup:
    !mkdir -p dist
    !npm install
    !npm run build
```

## Modifiers

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

## Interleaving with Prompts

Shell commands and LLM prompts can be **freely interleaved** in a single task. This is one of makethlm's defining features -- run a command, reason about its output, run another command:

```
task analyze:
    !git diff --name-only -> changed
    review these changed files for security issues:
    {{changed.stdout}}

    !npm test 2>&1 || true -> tests
    if tests failed, explain the root cause:
    {{tests.stdout}}
```

## Capturing and Piping Output

Use `-> name` on a shell step to expose that step's output to later prompts in
the same task:

```
task review:
    !git diff --name-only -> changed
    review {{changed.stdout}}
```

Every shell step also updates `{{last.stdout}}`, `{{last.exit_code}}`, and
`{{last.success}}`.

Use `|>` to prepend command output to the next LLM prompt:

```
task review:
    !git diff --name-only |>
    review the changed files for security issues
```

## Variable Interpolation

`{{name}}` interpolation works inside shell commands:

```
project := "myapp"

task build:
    !docker build -t {{quote(project)}}:latest .
```

Interpolation is textual; it does not shell-escape values automatically.
Wrap every variable or task argument that becomes a shell word with
`quote(...)`, especially values supplied on the command line:

```
task logs(service):
    !docker compose logs --tail=100 {{quote(service)}}
```

Keep shell operators and fixed syntax outside the quoted value.

## Echo Steps

Use `@echo` to output a message without invoking the LLM or running a shell command:

```
task deploy:
    @echo "Starting deployment..."
    !kubectl apply -f deployment.yaml
    @echo "Deployment complete"
```

Echo steps support variable interpolation:

```
task info:
    @echo "Deploying {{project}} version {{version}}"
```
