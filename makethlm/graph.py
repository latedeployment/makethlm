"""Task dependency resolution.

Both runners walk the same graph: ``topological_sort`` gives execution order and
``topological_levels`` groups independent tasks so they can run concurrently.
"""

from __future__ import annotations

from .models import Promptfile


class CycleError(Exception):
    """Raised when task dependencies contain a cycle."""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__(f"dependency cycle detected: {' -> '.join(cycle)}")


def topological_sort(pf: Promptfile, target: str) -> list[str]:
    """Return the tasks needed to run `target` in dependency order."""
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
        for dep in pf.tasks[name].subsequent_dependencies:
            visit(dep, path)

    visit(target, [])
    return order


def topological_levels(pf: Promptfile, target: str) -> list[list[str]]:
    """Return tasks grouped into levels for parallel execution.

    Tasks at the same level have no dependencies on each other and can
    be executed concurrently.
    """
    order = topological_sort(pf, target)
    if any(pf.tasks[name].subsequent_dependencies for name in order):
        return [[name] for name in order]

    # Build the in-degree map restricted to our subgraph
    order_set = set(order)
    completed: set[str] = set()
    remaining = list(order)
    levels: list[list[str]] = []

    while remaining:
        # A task is ready if all its deps (within our subgraph) are completed
        level: list[str] = []
        for name in remaining:
            deps = [d for d in pf.tasks[name].dependencies if d in order_set]
            if all(d in completed for d in deps):
                level.append(name)

        if not level:
            # Should never happen if topological_sort is correct, but safety valve
            break

        levels.append(level)
        for name in level:
            completed.add(name)
        remaining = [n for n in remaining if n not in completed]

    return levels
