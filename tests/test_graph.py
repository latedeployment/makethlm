"""Tests for task dependency resolution."""

from __future__ import annotations

import pytest

from makethlm.graph import CycleError, topological_levels, topological_sort
from makethlm.parser import parse


def _pf(text):
    return parse(text)


class TestTopologicalSort:
    def test_linear_chain(self):
        pf = _pf("task a:\n    !x\n\ntask b: a:\n    !x\n\ntask c: b:\n    !x\n")
        assert topological_sort(pf, "c") == ["a", "b", "c"]

    def test_diamond_visits_shared_dependency_once(self):
        pf = _pf(
            "task a:\n    !x\n\ntask b: a:\n    !x\n\ntask c: a:\n    !x\n\ntask d: b c:\n    !x\n"
        )
        order = topological_sort(pf, "d")
        assert order.count("a") == 1
        assert order.index("a") < order.index("b") < order.index("d")

    def test_target_without_dependencies(self):
        assert topological_sort(_pf("task solo:\n    !x\n"), "solo") == ["solo"]

    def test_cycle_is_reported_with_its_path(self):
        pf = _pf("task a: b:\n    !x\n\ntask b: a:\n    !x\n")
        with pytest.raises(CycleError) as excinfo:
            topological_sort(pf, "a")
        assert "a" in excinfo.value.cycle and "b" in excinfo.value.cycle
        assert "dependency cycle detected" in str(excinfo.value)

    def test_unrelated_tasks_are_excluded(self):
        pf = _pf("task a:\n    !x\n\ntask unrelated:\n    !x\n")
        assert topological_sort(pf, "a") == ["a"]


class TestTopologicalLevels:
    def test_independent_tasks_share_a_level(self):
        pf = _pf("task a:\n    !x\n\ntask b:\n    !x\n\ntask all: a b:\n    !x\n")
        levels = topological_levels(pf, "all")
        assert sorted(levels[0]) == ["a", "b"]
        assert levels[-1] == ["all"]

    def test_chain_is_one_task_per_level(self):
        pf = _pf("task a:\n    !x\n\ntask b: a:\n    !x\n")
        assert topological_levels(pf, "b") == [["a"], ["b"]]

    def test_subsequent_dependencies_force_serial_order(self):
        # `&&` dependencies must not be collapsed into a parallel level.
        pf = _pf("task a:\n    !x\n\ntask b:\n    !x\n\ntask all: a && b:\n    !x\n")
        levels = topological_levels(pf, "all")
        assert all(len(level) == 1 for level in levels)

    def test_every_task_appears_exactly_once(self):
        pf = _pf(
            "task a:\n    !x\n\ntask b: a:\n    !x\n\ntask c: a:\n    !x\n\ntask d: b c:\n    !x\n"
        )
        flat = [name for level in topological_levels(pf, "d") for name in level]
        assert sorted(flat) == ["a", "b", "c", "d"]
