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
        "task_args",
        nargs="*",
        default=[],
        help="Positional arguments for the task",
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
        help="Print prompts/commands that would be sent without executing",
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
            parts: list[str] = []
            if task.dependencies:
                parts.append(f"depends: {', '.join(task.dependencies)}")
            if task.arguments:
                arg_strs = []
                for a in task.arguments:
                    if a.default is not None:
                        arg_strs.append(f'{a.name}="{a.default}"')
                    else:
                        arg_strs.append(a.name)
                parts.append(f"args: {', '.join(arg_strs)}")
            if task.docker:
                parts.append(f"docker:{task.docker.tag}")
            if task.options.llm:
                parts.append(f"llm: {task.options.llm}")
            if task.options.on:
                parts.append(f"on: {task.options.on}")
            suffix = f" ({'; '.join(parts)})" if parts else ""
            first_line = task.prompt.split("\n")[0]
            if len(first_line) > 60:
                first_line = first_line[:57] + "..."
            print(f"  {name}{suffix}")
            print(f"    {first_line}")

        # List functions
        if pf.functions:
            print()
            print("  functions:")
            for fn_name, fn in pf.functions.items():
                first_line = fn.body.split("\n")[0]
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                print(f"    {fn_name}: {first_line}")

        # List LLM providers
        if pf.llm_providers:
            print()
            default = pf.default_llm
            print("  llm providers:")
            for pname, prov in pf.llm_providers.items():
                marker = " (default)" if pname == default else ""
                model_str = f" model={prov.model}" if prov.model else ""
                print(f"    {pname}{model_str}{marker}")

        # List host groups
        if pf.host_groups:
            print()
            print("  host groups:")
            for gname, group in pf.host_groups.items():
                user_str = f" user={group.user}" if group.user else ""
                port_str = f" port={group.port}" if group.port else ""
                print(f"    {gname}{user_str}{port_str}: {', '.join(group.hosts)}")

        return 0

    # Build dispatcher
    if args.dry_run:
        dispatcher = DryRunDispatcher()
    elif args.shell:
        dispatcher = ShellDispatcher(args.shell)
    else:
        dispatcher = ClaudeDispatcher(model=args.model)

    # Build task arguments dict from positional CLI args
    task_args: dict[str, str] | None = None
    target = args.task
    if target and target in pf.tasks and pf.tasks[target].arguments:
        task_def = pf.tasks[target]
        task_args = {}
        for idx, arg_def in enumerate(task_def.arguments):
            if idx < len(args.task_args):
                task_args[arg_def.name] = args.task_args[idx]
            elif arg_def.default is not None:
                task_args[arg_def.name] = arg_def.default
            else:
                print(
                    f"error: task {target!r} requires argument {arg_def.name!r}",
                    file=sys.stderr,
                )
                return 1

    # Run
    runner = Runner(pf, dispatcher)
    try:
        result = runner.run(target, args=task_args)
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
            for sr in tr.step_results:
                prefix = "!" if sr.kind == "shell" else ">"
                print(f"  {prefix} {sr.content}")
        else:
            for line in tr.response.strip().split("\n"):
                if line:
                    print(f"  {line}")
        print()

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
