# makethlm

**A task runner where tasks are LLM prompts.**

makethlm is a command-line task runner in the tradition of Make and Just, but
designed for a world where tasks are described in natural language and executed
by LLMs. Define your build, deploy, review, and maintenance workflows as
prose, interleave them with shell commands, and let your LLM of choice do the
heavy lifting.

```
# Promptfile

project := "my-web-app"

llm claude [model=opus]

task build:
    !mkdir -p dist
    check if src/ has changed since the last build.
    if so, compile the TypeScript and bundle with esbuild.

task test: build
    !npm test
    if any tests failed, explain the root cause and suggest/apply a fix.

task deploy(target, port="8080"): build test
    !systemctl restart {{project}}
    verify {{project}} is running on {{target}} port {{port}}.
```

```
$ makethlm deploy staging
[ok] build
  ...
[ok] test
  ...
[ok] deploy
  Verified my-web-app is running on staging port 8080. All health checks pass.
```

## Key Features

- **Natural language tasks** -- describe what you want in plain English, the LLM executes it
- **Shell interleaving** -- freely mix `!shell` commands with LLM prompts in a single task
- **Multi-provider LLM support** -- Claude, OpenAI, Ollama, or any CLI-based LLM
- **Task dependencies** -- topological sort with diamond/transitive dependency resolution
- **Task arguments** -- positional args with defaults and variadic support
- **Reusable functions** -- `fn`/`@use` for shared prompt templates
- **Docker generation** -- describe Docker images in prose, makethlm builds them
- **SSH host inventory** -- run shell commands on remote hosts via SSH
- **Reliable workflows** -- postmortems, rollback, typed artifact contracts,
  bounded provider fallback, reproducible cache keys, and redacted replay
- **Capability-first safety** -- inspect transitive shell, SSH, Docker, LLM,
  secret, and webhook requirements before execution
- **Namespaced modules** -- reuse tasks and their variables, providers, agents,
  hosts, guidance, aliases, and failure hooks without name collisions
- **Variable system** -- `{{var}}` interpolation, backtick execution, string functions, conditionals
- **Justfile-compatible** -- familiar syntax for Just users (`set` directives, `if/else`, built-in functions)

## Quick Example

A C project where the LLM generates a library and shell commands compile it:

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

task build:
    !gcc -c src/mylib.c -o src/mylib.o
    !gcc src/main.c src/mylib.o -o {{project}}

task run: build
    !./{{project}}
```

```bash
makethlm generate-lib   # LLM writes the C code
makethlm run            # compiles (via build dependency) and runs
makethlm --dry-run run  # preview without executing
makethlm --list         # see all tasks
```

## Next Steps

- [Getting Started](getting-started.md) -- install and write your first Promptfile
- [Syntax Reference](syntax.md) -- full language reference
- [CLI Reference](cli.md) -- command-line options
