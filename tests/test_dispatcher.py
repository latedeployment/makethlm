"""Tests for dispatchers."""

from justprompt.dispatcher import DryRunDispatcher
from justprompt.models import Task, TaskStep, TaskOptions


class TestDryRunDispatcher:
    def test_returns_success(self):
        d = DryRunDispatcher()
        task = Task(name="build", steps=[TaskStep(kind="prompt", content="build it")])
        result = d.dispatch("build it", task)
        assert result.success
        assert "build" in result.response

    def test_records_dispatches(self):
        d = DryRunDispatcher()
        t1 = Task(name="a", steps=[TaskStep(kind="prompt", content="do a")])
        t2 = Task(name="b", steps=[TaskStep(kind="prompt", content="do b")])
        d.dispatch("do a", t1)
        d.dispatch("do b", t2)
        assert len(d.dispatched) == 2
        assert d.dispatched[0] == ("do a", t1)
        assert d.dispatched[1] == ("do b", t2)

    def test_response_contains_task_name(self):
        d = DryRunDispatcher()
        task = Task(name="deploy", steps=[TaskStep(kind="prompt", content="deploy now")])
        result = d.dispatch("deploy now", task)
        assert "deploy" in result.response
