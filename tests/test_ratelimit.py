"""Tests for rate-limit backoff, provider concurrency caps, and progress output."""

from __future__ import annotations

import threading
import time

import pytest

from makethlm.dispatcher import Dispatcher, DispatchResult
from makethlm.parser import ParseError, parse
from makethlm.progress import ElapsedIndicator
from makethlm.ratelimit import (
    BASE_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    is_rate_limited,
    rate_limit_backoff,
)
from makethlm.runner import Runner


class TestRateLimitDetection:
    @pytest.mark.parametrize(
        "response",
        [
            "error: OpenAI API returned 429: slow down",
            "Rate limit exceeded",
            "rate_limit_error",
            "Too Many Requests",
            "quota exceeded for this project",
            "server overloaded, try again",
        ],
    )
    def test_detects_throttling(self, response):
        assert is_rate_limited(response)

    @pytest.mark.parametrize(
        "response",
        ["error: connection refused", "", "the number 4290 appears here"],
    )
    def test_ignores_other_failures(self, response):
        assert not is_rate_limited(response)


class TestBackoff:
    def test_grows_exponentially(self):
        assert rate_limit_backoff(1) == BASE_BACKOFF_SECONDS
        assert rate_limit_backoff(2) == BASE_BACKOFF_SECONDS * 2
        assert rate_limit_backoff(3) == BASE_BACKOFF_SECONDS * 4

    def test_is_capped(self):
        assert rate_limit_backoff(50) == MAX_BACKOFF_SECONDS

    def test_handles_first_attempt(self):
        assert rate_limit_backoff(0) == BASE_BACKOFF_SECONDS


class RateLimitedOnce(Dispatcher):
    """Fails with a 429 on the first call, then succeeds."""

    def __init__(self):
        self.calls = 0

    def dispatch(self, prompt, task):
        self.calls += 1
        if self.calls == 1:
            return DispatchResult(response="error: 429 rate limit", success=False)
        return DispatchResult(response="ok", success=True)


class TestRunnerBackoff:
    def test_waits_before_retrying_a_throttled_provider(self, monkeypatch):
        slept = []
        monkeypatch.setattr("makethlm.runner.time.sleep", lambda s: slept.append(s))
        pf = parse("""\
task review [retries=1]:
    review it
""")
        pf.default_llm = None
        dispatcher = RateLimitedOnce()
        result = Runner(pf, dispatcher, verbose=False).run("review")
        assert result.success
        assert dispatcher.calls == 2
        assert slept == [BASE_BACKOFF_SECONDS]

    def test_no_wait_for_other_failures(self, monkeypatch):
        slept = []
        monkeypatch.setattr("makethlm.runner.time.sleep", lambda s: slept.append(s))

        class Broken(Dispatcher):
            def __init__(self):
                self.calls = 0

            def dispatch(self, prompt, task):
                self.calls += 1
                return DispatchResult(response="error: connection refused", success=False)

        pf = parse("""\
task review [retries=1]:
    review it
""")
        pf.default_llm = None
        Runner(pf, Broken(), verbose=False).run("review")
        assert slept == []


class TestProviderConcurrency:
    def test_parses_max_concurrency(self):
        pf = parse("llm openai [model=x, max-concurrency=2]\n\ntask t:\n    do it\n")
        assert pf.llm_providers["openai"].max_concurrency == 2

    def test_rejects_zero(self):
        with pytest.raises(ParseError, match="must be at least 1"):
            parse("llm openai [max-concurrency=0]\n\ntask t:\n    do it\n")

    def test_rejects_non_integer(self):
        with pytest.raises(ParseError, match="must be an integer"):
            parse("llm openai [max-concurrency=many]\n\ntask t:\n    do it\n")

    def test_limit_is_enforced_across_parallel_tasks(self, monkeypatch):
        peak = {"current": 0, "max": 0}
        lock = threading.Lock()

        class SlowDispatcher(Dispatcher):
            def dispatch(self, prompt, task):
                with lock:
                    peak["current"] += 1
                    peak["max"] = max(peak["max"], peak["current"])
                time.sleep(0.05)
                with lock:
                    peak["current"] -= 1
                return DispatchResult(response="ok", success=True)

        dispatcher = SlowDispatcher()
        monkeypatch.setattr(
            "makethlm.runner._dispatcher_for_provider",
            lambda provider: dispatcher,
        )
        pf = parse("""\
llm capped [model=x, max-concurrency=1]

task a:
    first

task b:
    second

task c:
    third

task all: a b c:
    !echo done
""")
        result = Runner(pf, dispatcher, verbose=False).run_parallel("all", jobs=3)
        assert result.success
        assert peak["max"] == 1

    def test_without_a_limit_calls_overlap(self, monkeypatch):
        peak = {"current": 0, "max": 0}
        lock = threading.Lock()

        class SlowDispatcher(Dispatcher):
            def dispatch(self, prompt, task):
                with lock:
                    peak["current"] += 1
                    peak["max"] = max(peak["max"], peak["current"])
                time.sleep(0.05)
                with lock:
                    peak["current"] -= 1
                return DispatchResult(response="ok", success=True)

        dispatcher = SlowDispatcher()
        monkeypatch.setattr(
            "makethlm.runner._dispatcher_for_provider",
            lambda provider: dispatcher,
        )
        pf = parse("""\
llm uncapped [model=x]

task a:
    first

task b:
    second

task all: a b:
    !echo done
""")
        Runner(pf, dispatcher, verbose=False).run_parallel("all", jobs=2)
        assert peak["max"] == 2


class TestElapsedIndicator:
    def test_disabled_when_not_a_tty(self, capsys):
        with ElapsedIndicator("waiting", enabled=True, interval=0.01):
            time.sleep(0.05)
        # pytest captures stderr, so isatty() is False and nothing is drawn.
        assert capsys.readouterr().err == ""

    def test_disabled_explicitly(self):
        indicator = ElapsedIndicator("waiting", enabled=False)
        assert not indicator.enabled

    def test_context_manager_is_safe_to_reuse(self):
        indicator = ElapsedIndicator("waiting", enabled=False)
        with indicator:
            pass
        with indicator:
            pass
