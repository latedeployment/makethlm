# Repository Guidelines

## Project Structure & Module Organization

This repository contains the `makethlm` Python CLI package. Code lives in `makethlm/`:

- `cli.py` handles command-line parsing and user-facing output.
- `parser.py` parses `Promptfile` syntax into the model layer.
- `models.py` defines the Promptfile AST plus interpolation, functions, and conditions.
- `runner.py` executes tasks, dependencies, shell steps, SSH, Docker, caching, and webhooks.
- `dispatcher.py` routes prompt steps to dry-run, Claude CLI, or shell-template providers.

Tests live in `tests/`. Examples are under `examples/`, documentation under `docs/`, and MkDocs configuration is in `mkdocs.yml`. Treat `dist/`, `site/`, `*.egg-info`, `__pycache__/`, and compiled example outputs as generated.

## Build, Test, and Development Commands

- `uv run pytest tests/ -q --no-docker`: run the standard test suite without Docker-backed SSH integration tests.
- `uv run ruff check .`: run the local lint gate.
- `uv run pytest tests/test_parser.py -q`: run a focused parser test file.
- `uv run makethlm --help`: exercise the CLI entry point locally.
- `uv build --wheel`: build the package wheel.
- `./publish.sh --validate`: build and install the wheel in a temporary venv for a smoke test.
- `scripts/release.py patch --no-build`: preview version/changelog release automation without publishing.
- `uv run mkdocs serve`: preview docs locally when editing `docs/`.

## Coding Style & Naming Conventions

Use Python 3.10+ syntax and type hints consistent with existing code. Follow the current style: 4-space indentation, dataclasses for structured models, snake_case for functions and variables, PascalCase for classes, and short helpers near the code that uses them. Prefer standard-library solutions; this package has no declared runtime dependencies. Keep comments useful and sparse.

## Testing Guidelines

Tests use `pytest`. Name new test files `test_*.py` and test classes `TestFeatureName`. Add parser behavior to `tests/test_parser.py`, execution behavior to `tests/test_runner.py`, dispatcher behavior to `tests/test_dispatcher.py`, and inventory behavior to `tests/test_inventory.py`. Mark Docker/SSH tests as `integration` and keep them skippable with `--no-docker`.

## Commit & Pull Request Guidelines

Recent commits use concise summaries such as `Support dotenv path loading instead of only .env`. Prefer short, imperative or descriptive commit messages focused on one change.

Pull requests should include a clear description, mention affected syntax or CLI behavior, link related issues when available, and list tests run. Include docs updates for user-facing Promptfile or CLI changes.

## Security & Configuration Tips

Do not commit secrets, `.env` files, API keys, or generated credentials. Be careful with Promptfile backtick variables and shell steps: they execute local commands during parsing or task runs. Use dry runs when reviewing examples that call external LLM tools.

## Pre-Commit Security Review

Before committing code changes, run a security-focused review of the staged diff. Use `makethlm --dry-run pre-commit-security-review` to inspect the gate, then run the task or equivalent local checks. Look specifically for secret leakage, unsafe subprocess usage, path traversal, risky shell/SSH/Docker execution, and changes that weaken safe mode or redaction.
