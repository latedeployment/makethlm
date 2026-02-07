"""Task runner with dependency resolution via topological sort."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Promptfile, Task
from .dispatcher import Dispatcher, DispatchResult


class CycleError(Exception):
    """Raised when task dependencies contain a cycle."""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"dependency cycle detected: {' -> '.join(cycle)}")


def topological_sort(pf: Promptfile, target: str) -> list[str]:
    """Return the tasks needed to run `target` in dependency order.

    Raises CycleError if a cycle is detected.
    """
    order: list[str] = []
    visited: set[str] = set()
    in_stack: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in in_stack:
            cycle_start = path.index(name)
            raise CycleError(path[cycle_start:] + [name])
        if name in visited:
            return
        in_stack.add(name)
        path.append(name)
        for dep in pf.tasks[name].dependencies:
            visit(dep, path)
        path.pop()
        in_stack.remove(name)
        visited.add(name)
        order.append(name)

    visit(target, [])
    return order


@dataclass
class TaskResult:
    """Result of running a single task."""

    task_name: str
    prompt_sent: str
    response: str
    success: bool


@dataclass
class RunResult:
    """Result of running a target (including all dependencies)."""

    target: str
    task_results: list[TaskResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(r.success for r in self.task_results)


class Runner:
    """Executes tasks from a Promptfile using a Dispatcher."""

    def __init__(self, pf: Promptfile, dispatcher: Dispatcher):
        self.pf = pf
        self.dispatcher = dispatcher

    def run(self, target: str | None = None) -> RunResult:
        """Run a target task and all its dependencies.

        If target is None, runs the default (first) task.
        """
        if target is None:
            target = self.pf.default_task
            if target is None:
                raise ValueError("no tasks defined in Promptfile")

        if target not in self.pf.tasks:
            raise KeyError(f"unknown task: {target!r}")

        execution_order = topological_sort(self.pf, target)
        result = RunResult(target=target)

        for task_name in execution_order:
            task = self.pf.tasks[task_name]
            prompt = self.pf.resolve_prompt(task_name)

            dispatch_result = self.dispatcher.dispatch(prompt, task)
            task_result = TaskResult(
                task_name=task_name,
                prompt_sent=prompt,
                response=dispatch_result.response,
                success=dispatch_result.success,
            )
            result.task_results.append(task_result)

            if not task_result.success:
                break  # stop on first failure

        return result
