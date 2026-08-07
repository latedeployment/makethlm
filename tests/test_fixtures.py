"""Tests for recorded LLM fixtures (offline, deterministic runs)."""

from __future__ import annotations

import json

import pytest

from makethlm.dispatcher import Dispatcher, DispatchResult
from makethlm.fixtures import FixtureError, FixtureStore, fixture_key
from makethlm.parser import parse
from makethlm.runner import Runner


class CountingDispatcher(Dispatcher):
    """Records how many times a provider was actually called."""

    def __init__(self, response="live response", success=True):
        self.response = response
        self.success = success
        self.calls = 0

    def dispatch(self, prompt, task):
        self.calls += 1
        return DispatchResult(response=self.response, success=self.success)


PF = """\
task review:
    review the code
"""


class TestFixtureStore:
    def test_key_is_stable(self):
        assert fixture_key("t", "p") == fixture_key("t", "p")

    def test_key_separates_task_and_prompt(self):
        # Without a separator, ("ab", "c") and ("a", "bc") would collide.
        assert fixture_key("ab", "c") != fixture_key("a", "bc")

    def test_save_and_load_roundtrip(self, tmp_path):
        store = FixtureStore(tmp_path)
        store.save("review", "prompt", "answer", success=True, provider="claude")
        loaded = store.load("review", "prompt")
        assert loaded["response"] == "answer"
        assert loaded["success"] is True
        assert loaded["provider"] == "claude"

    def test_load_missing_returns_none(self, tmp_path):
        assert FixtureStore(tmp_path).load("review", "prompt") is None

    def test_malformed_fixture_raises(self, tmp_path):
        store = FixtureStore(tmp_path)
        path = store.path_for("review", "prompt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        with pytest.raises(FixtureError):
            store.load("review", "prompt")

    def test_fixture_is_owner_only(self, tmp_path):
        store = FixtureStore(tmp_path)
        path = store.save("review", "prompt", "answer", success=True)
        assert path.stat().st_mode & 0o077 == 0

    def test_count(self, tmp_path):
        store = FixtureStore(tmp_path)
        assert store.count() == 0
        store.save("review", "prompt", "answer", success=True)
        assert store.count() == 1


class TestRecording:
    def test_records_provider_response(self, tmp_path):
        dispatcher = CountingDispatcher()
        runner = Runner(
            parse(PF),
            dispatcher,
            verbose=False,
            fixtures_dir=str(tmp_path),
            record_fixtures=True,
        )
        result = runner.run("review")
        assert result.success
        assert dispatcher.calls == 1
        assert FixtureStore(tmp_path).count() == 1

    def test_recording_stores_redacted_response(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "supersecretvalue")
        pf = parse("""\
export API_TOKEN := "supersecretvalue"

task review:
    review the code
""")
        dispatcher = CountingDispatcher(response="leaked supersecretvalue here")
        runner = Runner(
            pf,
            dispatcher,
            verbose=False,
            fixtures_dir=str(tmp_path),
            record_fixtures=True,
        )
        runner.run("review")
        stored = json.loads(next(tmp_path.glob("*.json")).read_text())
        assert "supersecretvalue" not in stored["response"]


class TestReplay:
    def _record(self, tmp_path, response="recorded answer"):
        runner = Runner(
            parse(PF),
            CountingDispatcher(response=response),
            verbose=False,
            fixtures_dir=str(tmp_path),
            record_fixtures=True,
        )
        runner.run("review")

    def test_replay_does_not_call_provider(self, tmp_path):
        self._record(tmp_path)
        dispatcher = CountingDispatcher(response="should not be used")
        runner = Runner(
            parse(PF),
            dispatcher,
            verbose=False,
            fixtures_dir=str(tmp_path),
        )
        result = runner.run("review")
        assert result.success
        assert dispatcher.calls == 0
        assert result.task_results[0].response == "recorded answer"

    def test_missing_fixture_fails_closed(self, tmp_path):
        dispatcher = CountingDispatcher()
        runner = Runner(
            parse(PF),
            dispatcher,
            verbose=False,
            fixtures_dir=str(tmp_path / "empty"),
        )
        result = runner.run("review")
        assert not result.success
        assert dispatcher.calls == 0
        assert "no recorded fixture" in result.task_results[0].response

    def test_replay_is_deterministic(self, tmp_path):
        self._record(tmp_path)
        responses = set()
        for _ in range(3):
            runner = Runner(
                parse(PF),
                CountingDispatcher(response="ignored"),
                verbose=False,
                fixtures_dir=str(tmp_path),
            )
            responses.add(runner.run("review").task_results[0].response)
        assert responses == {"recorded answer"}

    def test_recorded_failure_replays_as_failure(self, tmp_path):
        runner = Runner(
            parse(PF),
            CountingDispatcher(response="provider exploded", success=False),
            verbose=False,
            fixtures_dir=str(tmp_path),
            record_fixtures=True,
        )
        runner.run("review")
        replay = Runner(
            parse(PF),
            CountingDispatcher(),
            verbose=False,
            fixtures_dir=str(tmp_path),
        )
        assert not replay.run("review").success

    def test_repair_prompts_are_recorded_separately(self, tmp_path):
        class ScriptedDispatcher(Dispatcher):
            def __init__(self):
                self.calls = 0

            def dispatch(self, prompt, task):
                self.calls += 1
                bad = "not json at all"
                return DispatchResult(
                    response='{"ok": true}' if self.calls > 1 else bad,
                    success=True,
                )

        pf_text = """\
task inspect [produces=object, repair=1]:
    return a JSON object
"""
        runner = Runner(
            parse(pf_text),
            ScriptedDispatcher(),
            verbose=False,
            fixtures_dir=str(tmp_path),
            record_fixtures=True,
        )
        assert runner.run("inspect").success
        # The original prompt and the repair prompt are distinct fixtures.
        assert FixtureStore(tmp_path).count() == 2

        replay = Runner(
            parse(pf_text),
            CountingDispatcher(response="unused"),
            verbose=False,
            fixtures_dir=str(tmp_path),
        )
        result = replay.run("inspect")
        assert result.success
        assert result.task_results[0].response == '{"ok": true}'
