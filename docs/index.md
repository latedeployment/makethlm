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

task build [sources="src/**/*.ts", outputs="dist/bundle.js"]:
    !mkdir -p dist
    compile the TypeScript in src/ and bundle it to dist/bundle.js with esbuild.

task test: build:
    !npm test
    if any tests failed, explain the root cause and suggest/apply a fix.

task deploy(target, port="8080"): build test:
    !systemctl restart {{project}}
    verify {{project}} is running on {{target}} port {{port}}.
```

```
$ makethlm deploy staging
[ok] build
  Bundled 14 TypeScript files into dist/bundle.js.
[ok] test
  All 32 tests passed.
[ok] deploy
  Verified my-web-app is running on staging port 8080. All health checks pass.
```

Run it again without touching `src/` and the build is skipped, because `sources`
and `outputs` give the task the same file-dependency tracking `make` has:

```
$ makethlm deploy staging
[ok] build
  [skipped] up to date (14 sources older than outputs)
```

## Key Features

- **Natural language tasks** -- describe what you want in plain English, the LLM executes it
- **Shell interleaving** -- freely mix `!shell` commands with LLM prompts in a single task
- **Multi-provider LLM support** -- Claude, Codex, opencode, OpenAI, Ollama, or any CLI-based LLM
- **Several models at once** -- fan one prompt out with `llm="a|b"`, keep every
  answer, and have a `judge` merge them
- **Task dependencies** -- topological sort with diamond/transitive dependency resolution
- **Incremental builds** -- `sources`/`outputs` skip a task whose outputs are
  newer than its inputs, with make-style automatic variables
- **Task arguments** -- positional args with defaults and variadic support
- **Reusable functions** -- `fn`/`@use` for shared prompt templates
- **Docker generation** -- describe Docker images in prose, makethlm builds them
- **SSH host inventory** -- run shell commands on remote hosts via SSH
- **Reliable workflows** -- postmortems, rollback, typed artifact contracts with
  `repair`, bounded provider fallback, reproducible cache keys, and redacted replay
- **Cost controls** -- token and spend accounting per run, with `--max-cost` budgets
- **Reproducible runs** -- record LLM responses once and replay them in CI with
  no credentials, no network, and no spend
- **MCP servers** -- declare once, attach per task, configured per invocation
- **Capability-first safety** -- inspect transitive shell, SSH, Docker, LLM,
  secret, MCP, and webhook requirements before execution
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

task run: build:
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
