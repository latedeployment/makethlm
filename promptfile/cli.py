"""CLI entry point for promptfile."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .parser import parse, ParseError
from .runner import Runner, CycleError
from .dispatcher import ClaudeDispatcher, DryRunDispatcher, ShellDispatcher


PROMPTFILE_NAMES = ["Promptfile", "promptfile", "Promptfile.pf", "promptfile.pf"]


def find_promptfile(directory: Path | None = None) -> Path | None:
    """Search for a Promptfile in the given directory (default: cwd)."""
    d = directory or Path.cwd()
    for name in PROMPTFILE_NAMES:
        candidate = d / name
        if candidate.is_file():
            return candidate
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="promptfile",
        description="A task runner where tasks are LLM prompts.",
    )
    ap.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Task to run (default: first task in file)",
    )
    ap.add_argument(
        "-f", "--file",
        type=Path,
        default=None,
        help="Path to Promptfile (default: auto-detect)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print prompts that would be sent without calling LLM",
    )
    ap.add_argument(
        "--list", "-l",
        action="store_true",
        dest="list_tasks",
        help="List available tasks and exit",
    )
    ap.add_argument(
        "--model", "-m",
        default=None,
        help="Default LLM model to use",
    )
    ap.add_argument(
        "--shell",
        default=None,
        help='Shell template for LLM CLI, e.g. \'openai chat -p "{prompt}"\'',
    )
    ap.add_argument(
        "--var", "-V",
        action="append",
        default=[],
        help="Override a variable: -V name=value",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    # Locate the Promptfile
    pf_path: Path | None = args.file
    if pf_path is None:
        pf_path = find_promptfile()
    if pf_path is None or not pf_path.is_file():
        print("error: no Promptfile found", file=sys.stderr)
        return 1

    # Parse
    try:
        source = pf_path.read_text()
        pf = parse(source, filename=str(pf_path))
    except ParseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Apply variable overrides from CLI
    for var_str in args.var:
        if "=" not in var_str:
            print(f"error: invalid --var format (expected name=value): {var_str!r}", file=sys.stderr)
            return 1
        key, value = var_str.split("=", 1)
        pf.variables[key.strip()] = value.strip()

    # List mode
    if args.list_tasks:
        for name in pf.task_order:
            task = pf.tasks[name]
            deps = f" (depends: {', '.join(task.dependencies)})" if task.dependencies else ""
            first_line = task.prompt.split("\n")[0]
            if len(first_line) > 60:
                first_line = first_line[:57] + "..."
            print(f"  {name}{deps}")
            print(f"    {first_line}")
        return 0

    # Build dispatcher
    if args.dry_run:
        dispatcher = DryRunDispatcher()
    elif args.shell:
        dispatcher = ShellDispatcher(args.shell)
    else:
        dispatcher = ClaudeDispatcher(model=args.model)

    # Run
    runner = Runner(pf, dispatcher)
    try:
        result = runner.run(args.task)
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except CycleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # Print results
    for tr in result.task_results:
        status = "ok" if tr.success else "FAILED"
        print(f"[{status}] {tr.task_name}")
        if args.dry_run:
            print(f"  prompt: {tr.prompt_sent}")
        else:
            for line in tr.response.strip().split("\n"):
                print(f"  {line}")
        print()

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
