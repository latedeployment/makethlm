"""Tests for the per-call LLM debug log."""

from __future__ import annotations

import json

from makethlm.calllog import MAX_LOGGED_CHARS, CallLog, CallRecord
from makethlm.dispatcher import Dispatcher, DispatchResult
from makethlm.parser import parse
from makethlm.runner import Runner


def _record(**overrides):
    base = dict(
        task="review",
        provider="claude",
        kind="prompt",
        attempt=1,
        success=True,
        duration_ms=12,
        prompt="p",
        response="r",
    )
    base.update(overrides)
    return CallRecord(**base)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestCallLog:
    def test_appends_one_line_per_record(self, tmp_path):
        log = CallLog(tmp_path / "calls.jsonl")
        log.record(_record())
        log.record(_record(attempt=2))
        assert [entry["attempt"] for entry in _lines(tmp_path / "calls.jsonl")] == [1, 2]

    def test_creates_parent_directories(self, tmp_path):
        log = CallLog(tmp_path / "nested" / "deep" / "calls.jsonl")
        log.record(_record())
        assert (tmp_path / "nested" / "deep" / "calls.jsonl").is_file()

    def test_file_is_owner_only(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        CallLog(path).record(_record())
        assert path.stat().st_mode & 0o077 == 0

    def test_truncates_huge_payloads(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        CallLog(path).record(_record(prompt="x" * (MAX_LOGGED_CHARS * 2)))
        entry = _lines(path)[0]
        assert "truncated" in entry["prompt"]
        assert len(entry["prompt"]) < MAX_LOGGED_CHARS * 2

    def test_a_broken_log_never_raises(self, tmp_path):
        # A directory where the file should be: writing must fail silently.
        path = tmp_path / "calls.jsonl"
        path.mkdir()
        log = CallLog(path)
        log.record(_record())
        log.record(_record())


class CountingDispatcher(Dispatcher):
    def __init__(self, success=True):
        self.success = success

    def dispatch(self, prompt, task):
        return DispatchResult(
            response="answer",
            success=self.success,
            tokens_in=10,
            tokens_out=5,
        )


PF = """\
task review:
    review the code
"""


class TestRunnerLogging:
    def test_records_each_call(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        runner = Runner(parse(PF), CountingDispatcher(), verbose=False, call_log_path=str(path))
        runner.run("review")
        entries = _lines(path)
        assert len(entries) == 1
        assert entries[0]["task"] == "review"
        assert entries[0]["tokens_in"] == 10
        assert entries[0]["kind"] == "prompt"
        assert entries[0]["source"] == "provider"

    def test_no_log_without_a_path(self, tmp_path):
        runner = Runner(parse(PF), CountingDispatcher(), verbose=False)
        runner.run("review")
        assert list(tmp_path.iterdir()) == []

    def test_repair_calls_are_labeled(self, tmp_path):
        class Scripted(Dispatcher):
            def __init__(self):
                self.calls = 0

            def dispatch(self, prompt, task):
                self.calls += 1
                return DispatchResult(
                    response='{"ok": true}' if self.calls > 1 else "not json",
                    success=True,
                )

        path = tmp_path / "calls.jsonl"
        pf = parse("""\
task inspect [produces=object, repair=1]:
    return an object
""")
        Runner(pf, Scripted(), verbose=False, call_log_path=str(path)).run("inspect")
        assert [entry["kind"] for entry in _lines(path)] == ["prompt", "repair"]

    def test_failed_calls_are_recorded(self, tmp_path):
        path = tmp_path / "calls.jsonl"
        runner = Runner(
            parse(PF),
            CountingDispatcher(success=False),
            verbose=False,
            call_log_path=str(path),
        )
        runner.run("review")
        assert _lines(path)[0]["success"] is False

    def test_replayed_fixtures_are_marked(self, tmp_path):
        fixtures = tmp_path / "fx"
        Runner(
            parse(PF),
            CountingDispatcher(),
            verbose=False,
            fixtures_dir=str(fixtures),
            record_fixtures=True,
        ).run("review")

        path = tmp_path / "calls.jsonl"
        Runner(
            parse(PF),
            CountingDispatcher(),
            verbose=False,
            fixtures_dir=str(fixtures),
            call_log_path=str(path),
        ).run("review")
        assert _lines(path)[0]["source"] == "fixture"

    def test_secrets_are_redacted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("API_TOKEN", "supersecretvalue")
        path = tmp_path / "calls.jsonl"
        pf = parse("""\
export API_TOKEN := "supersecretvalue"

task review:
    review with {{API_TOKEN}}
""")

        class Echo(Dispatcher):
            def dispatch(self, prompt, task):
                return DispatchResult(response=prompt, success=True)

        Runner(pf, Echo(), verbose=False, call_log_path=str(path)).run("review")
        body = path.read_text()
        assert "supersecretvalue" not in body
