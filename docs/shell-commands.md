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
    !git diff --name-only > /tmp/changed.txt
    review the changed files listed in /tmp/changed.txt for security issues
    !npm test
    if any tests failed, explain the root cause
```

## Variable Interpolation

`{{name}}` interpolation works inside shell commands:

```
project := "myapp"

task build:
    !docker build -t {{project}}:latest .
```

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
