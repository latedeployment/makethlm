# makethlm

**A task runner where tasks are LLM prompts.**

[![PyPI version](https://img.shields.io/pypi/v/makethlm)](https://pypi.org/project/makethlm/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-latedeployment.github.io-blue)](https://latedeployment.github.io/makethlm/)

makethlm is a command-line task runner in the tradition of
[Make](https://www.gnu.org/software/make/) and [Just](https://github.com/casey/just),
but designed for a world where tasks are described in natural language and
executed by LLMs. Define your build, deploy, review, and maintenance workflows as
prose, interleave them with shell commands, and let your LLM of choice do the
heavy lifting.

**Full documentation: <https://latedeployment.github.io/makethlm/>**

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

Run it again without touching `src/` and the build is skipped — `sources` and
`outputs` give a task the same file-dependency tracking `make` has, so no LLM
call is spent working out that nothing changed:

```
$ makethlm deploy staging
[ok] build
  [skipped] up to date (14 sources older than outputs)
```

---

## Installation

```bash
pip install makethlm
```

Requires Python 3.10 or newer.

By default, makethlm uses the [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli)
as its LLM backend. Make sure it is installed and authenticated:

```bash
claude --version
```

Codex, opencode, native OpenAI, and native Ollama are also supported — see
[LLM Providers](https://latedeployment.github.io/makethlm/llm-providers/).

---

## Quick Start

Create a `Promptfile`:

```
project := "hello"

task greet(name="world"):
    !echo "starting"
    write a short friendly greeting for {{name}}
```

Then run it:

```bash
makethlm greet Ada        # run a task with an argument
makethlm --dry-run greet  # preview prompts and commands without executing
makethlm --list           # see every task, function, provider, and host group
```

Lines starting with `!` are shell commands. Everything else is a prompt sent to
the LLM. Tasks depend on each other with `task test: build:`.

More in [Getting Started](https://latedeployment.github.io/makethlm/getting-started/),
and runnable projects under [`examples/`](examples/).

---

## What it can do

**Incremental builds.** `sources` and `outputs` skip a task whose outputs are
already newer than its inputs, and `{{makethlm_changed}}` (make's `$?`) holds
just the files that moved:

```
task lint [sources="src/**/*.py", outputs=".lint-stamp"]:
    !ruff check {{makethlm_changed}}
    !touch .lint-stamp
```

**Several models at once.** Fan one prompt out to multiple providers, keep every
answer, and optionally have one model merge them:

```
task review [llm="claude|openai|local", judge=claude]:
    review this diff for security bugs
```

**Costs you can bound.** Token and spend accounting per run, with a stop-loss:

```bash
makethlm --max-cost 2.50 review
```

**Runs you can reproduce.** Record real LLM responses once, then replay them in
CI with no credentials, no network, and no spend:

```bash
makethlm --fixtures tests/fixtures --record-fixtures review   # once
makethlm --fixtures tests/fixtures review                     # in CI
```

**Output you can rely on.** `produces` enforces a task's output type, and
`repair` re-prompts when a model breaks the contract:

```
task inspect [produces=object, repair=1]:
    return the deployment report as a JSON object
```

**Tools via MCP.** Declare servers once and attach them per task; each provider
is configured for that invocation only, never your global config:

```
mcp github [url=https://api.githubcopilot.com/mcp/]

task review [mcp=github]:
    review the open pull request
```

**Safety you can inspect.** See what a task can reach before running it, and
require each capability explicitly:

```bash
makethlm --capabilities deploy
makethlm --safe --allow-shell --allow-llm deploy
```

Also: SSH host inventories, Docker image generation from prose, reusable prompt
functions, modules, secrets injection, run history and replay, `--watch`,
`--parallel`, and a `makethlm fmt` formatter — all covered in the
[documentation](https://latedeployment.github.io/makethlm/).

---

## Documentation

| Page | Covers |
|------|--------|
| [Getting Started](https://latedeployment.github.io/makethlm/getting-started/) | Install, first Promptfile, first run |
| [Syntax Reference](https://latedeployment.github.io/makethlm/syntax/) | The whole file format |
| [Tasks](https://latedeployment.github.io/makethlm/tasks/) | Dependencies, arguments, every task option |
| [Variables](https://latedeployment.github.io/makethlm/variables/) | Interpolation, functions, automatic variables |
| [Shell Commands](https://latedeployment.github.io/makethlm/shell-commands/) | `!` steps, capture, piping into prompts |
| [LLM Providers](https://latedeployment.github.io/makethlm/llm-providers/) | Claude, Codex, opencode, OpenAI, Ollama, MCP, fan-out |
| [Reliable Workflows](https://latedeployment.github.io/makethlm/reliability/) | Staleness, contracts, repair, fixtures, budgets, replay |
| [CLI Reference](https://latedeployment.github.io/makethlm/cli/) | Every flag |
| [Security](https://latedeployment.github.io/makethlm/security/) | Safe mode, sandboxing, redaction |
| [Secrets](https://latedeployment.github.io/makethlm/secrets-injection/) | Env, Infisical, 1Password, SOPS |
| [Just Compatibility](https://latedeployment.github.io/makethlm/just-compatibility/) | What carries over from justfiles |

---

## Comparison with Make and Just

| Feature | Make | Just | makethlm |
|---------|------|------|----------|
| Task body language | Shell commands | Shell commands | Natural language + shell |
| LLM integration | None | None | First-class, multi-provider |
| Dependencies | File-based (mtime) | Task-based | Task-based + file-based (`sources`/`outputs`) |
| Automatic variables | `$@`, `$^`, `$?` | N/A | `{{makethlm_task}}`, `{{makethlm_deps}}`, `{{makethlm_changed}}` |
| Variable interpolation | `$(VAR)` | `{{VAR}}` | `{{VAR}}` |
| Task arguments | None | Positional + defaults + variadic | Positional + defaults + variadic |
| Shell command prefix | (tab-indented) | (indented) | `!` prefix |
| Reusable templates | None | None | `fn` / `@use` |
| Docker generation | None | None | `docker` blocks |
| Remote execution | None | None | SSH host inventory |
| Multi-LLM routing | N/A | N/A | Per-task `[llm=...]`, fan-out, `judge` |
| Cost controls | N/A | N/A | Usage accounting + `--max-cost` |
| Reproducible runs | N/A | N/A | Recorded fixtures, run replay |
| File composition | `include` | `import` | `include` / `import` / `mod` |
| Dry run | `-n` | `--dry-run` | `--dry-run` |
| File name | `Makefile` | `justfile` | `Promptfile` |

---

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/ -q --no-docker
uv run mypy
./publish.sh --validate --skip-tests
```

Use these checks before committing changes that touch parser, runner, CLI, or
packaging behavior. To preview the documentation site:

```bash
uv run --group docs mkdocs serve
```

Release preparation is scripted:

```bash
scripts/release.py patch        # bump, test, validate, commit, tag
scripts/release.py minor --publish
```

---

## License

MIT
