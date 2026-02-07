"""CLI entry point for makethlm."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .parser import parse, ParseError
from .runner import Runner, CycleError, topological_sort, _dispatcher_for_provider
from .dispatcher import ClaudeDispatcher, Dispatcher, DryRunDispatcher, ShellDispatcher
from .models import Promptfile, _evaluate_expression, _builtin_functions


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
        prog="makethlm",
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
        "--summary", "-s",
        action="store_true",
        help="List task names only (compact, one per line)",
    )
    ap.add_argument(
        "--dump",
        action="store_true",
        help="Dump the parsed Promptfile (variables, tasks, etc.)",
    )
    ap.add_argument(
        "--evaluate",
        metavar="EXPR",
        default=None,
        help="Evaluate an expression and print the result",
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
    ap.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress command echoing",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output (show all step details)",
    )
    return ap


def _validate_tools(dispatcher: Dispatcher, pf, target: str | None) -> list[str]:
    """Validate that all required LLM CLI tools are installed.

    Returns a list of error messages (empty if everything is fine).
    """
    errors: list[str] = []
    checked: set[str] = set()

    def _check(d: Dispatcher, label: str) -> None:
        key = repr(d)
        if key in checked:
            return
        checked.add(key)
        err = d.validate_tool()
        if err:
            errors.append(f"{err} (needed by {label})")

    # Validate the default dispatcher
    _check(dispatcher, "default dispatcher")

    # Determine which tasks will actually execute
    if target is None:
        target = pf.default_task
    if target is None:
        return errors

    target = pf.resolve_alias(target)
    if target not in pf.tasks:
        return errors

    try:
        execution_order = topological_sort(pf, target)
    except CycleError:
        return errors  # cycle errors are reported later

    for task_name in execution_order:
        task = pf.tasks[task_name]
        # Skip shell-only tasks — they don't invoke the LLM
        has_prompt = any(s.kind == "prompt" for s in task.steps)
        has_docker = task.docker is not None
        if not has_prompt and not has_docker:
            continue

        # Check per-task LLM dispatcher if configured
        provider = pf.get_llm_for_task(task_name)
        if provider:
            per_task_dispatcher = _dispatcher_for_provider(provider)
            _check(per_task_dispatcher, f"task '{task_name}'")

    return errors


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

    # --evaluate: evaluate an expression
    if args.evaluate is not None:
        context = _builtin_functions()
        context.update(pf.variables)
        result = _evaluate_expression(args.evaluate, context)
        print(result)
        return 0

    # --summary: compact task list
    if args.summary:
        for name in pf.task_order:
            task = pf.tasks[name]
            if not task.options.private:
                print(name)
        return 0

    # --dump: dump parsed structure
    if args.dump:
        print("variables:")
        for k, v in pf.variables.items():
            exported = " (exported)" if k in pf.exported_vars else ""
            print(f"  {k} = {v!r}{exported}")
        if pf.settings.export:
            print("  (set export: all variables exported)")
        print()
        print("settings:")
        for field_name in ("dotenv_load", "shell", "working_dir", "export",
                           "positional_arguments", "ignore_comments",
                           "tempdir", "quiet"):
            val = getattr(pf.settings, field_name)
            if val:
                print(f"  {field_name} = {val!r}")
        print()
        if pf.guidance:
            print("guidance:")
            for gline in pf.guidance.split("\n")[:5]:
                print(f"  {gline}")
            if pf.guidance.count("\n") > 4:
                print(f"  ... ({pf.guidance.count(chr(10)) + 1} lines total)")
            print()
        if pf.agents:
            print("agents:")
            for aname, agent in pf.agents.items():
                parts = [agent.instructions_path]
                if agent.llm:
                    parts.append(f"llm={agent.llm}")
                if agent.model:
                    parts.append(f"model={agent.model}")
                print(f"  {aname} ({', '.join(parts)})")
            print()
        print("tasks:")
        for name in pf.task_order:
            task = pf.tasks[name]
            flags = []
            if task.options.private:
                flags.append("private")
            if task.options.agent:
                flags.append(f"agent={task.options.agent}")
            if task.options.os_filter:
                flags.append(f"os={task.options.os_filter}")
            if task.options.no_cd:
                flags.append("no-cd")
            if task.options.confirm:
                flags.append("confirm")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            deps_str = f" : {' '.join(task.dependencies)}" if task.dependencies else ""
            args_str = ""
            if task.arguments:
                parts = []
                for a in task.arguments:
                    prefix = a.variadic or ""
                    if a.default is not None:
                        parts.append(f'{prefix}{a.name}="{a.default}"')
                    else:
                        parts.append(f"{prefix}{a.name}")
                args_str = f"({', '.join(parts)})"
            print(f"  {name}{args_str}{deps_str}{flag_str}")
        return 0

    # List mode
    if args.list_tasks:
        # Collect tasks by group, filtering out private tasks
        ungrouped: list[str] = []
        groups: dict[str, list[str]] = {}
        for name in pf.task_order:
            task = pf.tasks[name]
            if task.options.private:
                continue
            group = task.options.group
            if group:
                groups.setdefault(group, []).append(name)
            else:
                ungrouped.append(name)

        def _print_task(name: str) -> None:
            task = pf.tasks[name]
            parts: list[str] = []
            if task.dependencies:
                parts.append(f"depends: {', '.join(task.dependencies)}")
            if task.arguments:
                arg_strs = []
                for a in task.arguments:
                    prefix = a.variadic or ""
                    if a.default is not None:
                        arg_strs.append(f'{prefix}{a.name}="{a.default}"')
                    else:
                        arg_strs.append(f"{prefix}{a.name}")
                parts.append(f"args: {', '.join(arg_strs)}")
            if task.docker:
                parts.append(f"docker:{task.docker.tag}")
            if task.options.llm:
                parts.append(f"llm: {task.options.llm}")
            if task.options.agent:
                parts.append(f"agent: {task.options.agent}")
            if task.options.on:
                parts.append(f"on: {task.options.on}")
            if task.options.os_filter:
                parts.append(f"os: {task.options.os_filter}")
            suffix = f" ({'; '.join(parts)})" if parts else ""
            doc = task.options.doc
            if doc:
                desc = doc
            else:
                desc = task.prompt.split("\n")[0]
            if len(desc) > 60:
                desc = desc[:57] + "..."
            print(f"  {name}{suffix}")
            print(f"    {desc}")

        for name in ungrouped:
            _print_task(name)

        for group_name, task_names in groups.items():
            print()
            print(f"  [{group_name}]")
            for name in task_names:
                _print_task(name)

        # List aliases
        if pf.aliases:
            print()
            print("  aliases:")
            for alias_name, alias_target in pf.aliases.items():
                print(f"    {alias_name} -> {alias_target}")

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

        # List guidance
        if pf.guidance:
            print()
            first_line = pf.guidance.split("\n")[0]
            if len(first_line) > 60:
                first_line = first_line[:57] + "..."
            print(f"  guidance: {first_line}")

        # List agents
        if pf.agents:
            print()
            print("  agents:")
            for aname, agent in pf.agents.items():
                parts = []
                if agent.llm:
                    parts.append(f"llm={agent.llm}")
                if agent.model:
                    parts.append(f"model={agent.model}")
                suffix = f" ({', '.join(parts)})" if parts else ""
                first_line = agent.instructions.split("\n")[0]
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                print(f"    {aname}{suffix}: {first_line}")

        return 0

    # Build dispatcher
    if args.dry_run:
        dispatcher = DryRunDispatcher()
    elif args.shell:
        dispatcher = ShellDispatcher(args.shell)
    else:
        dispatcher = ClaudeDispatcher(model=args.model)

    # Validate that required CLI tools are installed
    tool_errors = _validate_tools(dispatcher, pf, args.task)
    if tool_errors:
        for err in tool_errors:
            print(err, file=sys.stderr)
        return 1

    # Build task arguments dict from positional CLI args
    task_args: dict[str, str] | None = None
    target = args.task
    # Resolve alias
    if target:
        target = pf.resolve_alias(target)
    if target and target in pf.tasks and pf.tasks[target].arguments:
        task_def = pf.tasks[target]
        task_args = {}
        for idx, arg_def in enumerate(task_def.arguments):
            if arg_def.variadic:
                # Variadic: collect remaining args
                remaining = args.task_args[idx:]
                if arg_def.variadic == "+" and not remaining:
                    print(
                        f"error: task {target!r} requires at least one value for +{arg_def.name}",
                        file=sys.stderr,
                    )
                    return 1
                task_args[arg_def.name] = " ".join(remaining)
                break
            elif idx < len(args.task_args):
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
    runner = Runner(pf, dispatcher, quiet=args.quiet, verbose=not args.dry_run, promptfile_path=str(pf_path))
    try:
        result = runner.run(target, args=task_args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
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
