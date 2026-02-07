"""Tests for the task runner and dependency resolution."""

import pytest

from promptfile.parser import parse
from promptfile.runner import Runner, topological_sort, CycleError
from promptfile.dispatcher import DryRunDispatcher


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------

class TestTopologicalSort:
    def test_no_deps(self):
        pf = parse("""\
task build:
    build it
""")
        assert topological_sort(pf, "build") == ["build"]

    def test_linear_deps(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b

task c: b:
    do c
""")
        order = topological_sort(pf, "c")
        assert order == ["a", "b", "c"]

    def test_diamond_deps(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b

task c: a:
    do c

task d: b c:
    do d
""")
        order = topological_sort(pf, "d")
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")
        assert len(order) == 4

    def test_cycle_detection(self):
        # We can't create a cycle through the parser (it validates deps exist
        # but not cycles), so build one manually.
        pf = parse("""\
task a:
    do a

task b:
    do b
""")
        # Inject a cycle: a -> b -> a
        pf.tasks["a"].dependencies = ["b"]
        pf.tasks["b"].dependencies = ["a"]

        with pytest.raises(CycleError, match="cycle"):
            topological_sort(pf, "a")

    def test_self_cycle(self):
        pf = parse("""\
task a:
    do a
""")
        pf.tasks["a"].dependencies = ["a"]

        with pytest.raises(CycleError):
            topological_sort(pf, "a")

    def test_only_needed_tasks_included(self):
        pf = parse("""\
task a:
    do a

task b:
    do b

task c: a:
    do c
""")
        # Running 'c' should only include 'a' and 'c', not 'b'
        order = topological_sort(pf, "c")
        assert "b" not in order
        assert order == ["a", "c"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TestRunner:
    def test_run_single_task(self):
        pf = parse("""\
task build:
    build the project
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("build")

        assert result.success
        assert len(result.task_results) == 1
        assert result.task_results[0].task_name == "build"
        assert result.task_results[0].prompt_sent == "build the project"

    def test_run_default_task(self):
        pf = parse("""\
task first:
    first task prompt

task second:
    second task prompt
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run()  # no target — should run 'first'

        assert result.target == "first"
        assert len(result.task_results) == 1
        assert result.task_results[0].task_name == "first"

    def test_run_with_deps_executes_in_order(self):
        pf = parse("""\
task build:
    build

task test: build:
    test

task deploy: test:
    deploy
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        assert result.success
        names = [r.task_name for r in result.task_results]
        assert names == ["build", "test", "deploy"]

    def test_run_with_variable_interpolation(self):
        pf = parse("""\
project := "acme"

task deploy:
    deploy {{project}} now
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("deploy")

        assert result.task_results[0].prompt_sent == "deploy acme now"

    def test_run_unknown_task_raises(self):
        pf = parse("""\
task build:
    build
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with pytest.raises(KeyError, match="unknown task"):
            runner.run("nonexistent")

    def test_run_empty_promptfile_raises(self):
        pf = parse("")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)

        with pytest.raises(ValueError, match="no tasks defined"):
            runner.run()

    def test_dispatcher_receives_all_tasks(self):
        pf = parse("""\
task a:
    do a

task b: a:
    do b
""")
        dispatcher = DryRunDispatcher()
        runner = Runner(pf, dispatcher)
        runner.run("b")

        assert len(dispatcher.dispatched) == 2
        assert dispatcher.dispatched[0][1].name == "a"
        assert dispatcher.dispatched[1][1].name == "b"

    def test_failure_stops_execution(self):
        """If a task fails, subsequent tasks should not run."""
        pf = parse("""\
task a:
    do a

task b: a:
    do b
""")

        class FailDispatcher(DryRunDispatcher):
            def dispatch(self, prompt, task):
                result = super().dispatch(prompt, task)
                if task.name == "a":
                    result.success = False
                return result

        dispatcher = FailDispatcher()
        runner = Runner(pf, dispatcher)
        result = runner.run("b")

        assert not result.success
        assert len(result.task_results) == 1  # only 'a' ran
        assert result.task_results[0].task_name == "a"
