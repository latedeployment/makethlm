"""Tests for multi-model fan-out, judging, and prompt-to-prompt chaining."""

from __future__ import annotations

import threading
import time

import pytest

from makethlm.dispatcher import Dispatcher, DispatchResult
from makethlm.parser import ParseError, parse
from makethlm.runner import Runner, build_judge_prompt, format_fanout_response


class NamedDispatcher(Dispatcher):
    """Answers with its own name so responses are attributable."""

    def __init__(self, name, success=True, delay=0.0):
        self.name = name
        self.success = success
        self.delay = delay
        self.prompts: list[str] = []

    def dispatch(self, prompt, task):
        self.prompts.append(prompt)
        if self.delay:
            time.sleep(self.delay)
        if "Merge their answers" in prompt:
            return DispatchResult(response=f"merged by {self.name}", success=self.success)
        return DispatchResult(response=f"answer from {self.name}", success=self.success)


@pytest.fixture
def providers(monkeypatch):
    """Route each declared provider to its own dispatcher."""
    registry: dict[str, NamedDispatcher] = {}

    def install(*names, **overrides):
        for name in names:
            registry[name] = overrides.get(name) or NamedDispatcher(name)
        monkeypatch.setattr(
            "makethlm.runner._dispatcher_for_provider",
            lambda provider: registry[provider.name],
        )
        return registry

    return install


PROVIDER_DECLS = """\
llm alpha [model=a]
llm beta [model=b]
llm gamma [model=c]
"""


class TestParsingFanout:
    def test_pipe_separated_providers(self):
        pf = parse(PROVIDER_DECLS + '\ntask review [llm="alpha|beta"]:\n    review\n')
        options = pf.tasks["review"].options
        assert options.llms == ["alpha", "beta"]
        assert options.llm == "alpha"

    def test_single_provider_still_works(self):
        pf = parse(PROVIDER_DECLS + "\ntask review [llm=alpha]:\n    review\n")
        options = pf.tasks["review"].options
        assert options.llm == "alpha"
        assert options.llms == ["alpha"]

    def test_judge_is_parsed(self):
        pf = parse(PROVIDER_DECLS + '\ntask review [llm="alpha|beta", judge=gamma]:\n    review\n')
        assert pf.tasks["review"].options.judge == "gamma"

    def test_judge_without_fanout_is_rejected(self):
        with pytest.raises(ParseError, match="judge without a fan-out"):
            parse(PROVIDER_DECLS + "\ntask review [llm=alpha, judge=gamma]:\n    review\n")

    def test_empty_provider_list_is_rejected(self):
        with pytest.raises(ParseError, match="at least one provider"):
            parse(PROVIDER_DECLS + '\ntask review [llm="|"]:\n    review\n')

    def test_too_many_providers_rejected(self):
        many = "|".join(f"p{i}" for i in range(9))
        with pytest.raises(ParseError, match="at most"):
            parse(f'task review [llm="{many}"]:\n    review\n')


class TestParsingChain:
    def test_llm_directive_sets_step_provider(self):
        pf = parse("task t:\n    @llm alpha\n    do it\n")
        step = pf.tasks["t"].steps[0]
        assert step.kind == "prompt"
        assert step.llm == "alpha"

    def test_directive_applies_to_later_steps_only(self):
        pf = parse("task t:\n    first\n    @llm alpha\n    second\n")
        steps = pf.tasks["t"].steps
        assert steps[0].llm is None
        assert steps[1].llm == "alpha"

    def test_prompt_pipe_splits_steps(self):
        pf = parse("task t:\n    draft it |>\n    tighten it\n")
        steps = pf.tasks["t"].steps
        assert len(steps) == 2
        assert steps[0].content == "draft it"
        assert steps[0].pipe_output is True
        assert steps[1].pipe_output is False

    def test_bare_llm_directive_clears_the_override(self):
        pf = parse("task t:\n    @llm alpha\n    first |>\n    @llm\n    second\n")
        steps = pf.tasks["t"].steps
        assert steps[0].llm == "alpha"
        assert steps[1].llm is None


class TestFanoutExecution:
    def _pf(self, options=""):
        return parse(PROVIDER_DECLS + f"\ntask review [{options}]:\n    review this\n")

    def test_every_provider_is_called(self, providers):
        registry = providers("alpha", "beta", "gamma")
        pf = self._pf('llm="alpha|beta|gamma"')
        result = Runner(pf, registry["alpha"], verbose=False).run("review")
        assert result.success
        assert all(registry[name].prompts for name in ("alpha", "beta", "gamma"))

    def test_all_answers_are_reported(self, providers):
        providers("alpha", "beta")
        pf = self._pf('llm="alpha|beta"')
        response = Runner(pf, NamedDispatcher("x"), verbose=False).run("review")
        text = response.task_results[0].response
        assert "answer from alpha" in text
        assert "answer from beta" in text

    def test_each_answer_is_its_own_artifact(self, providers):
        providers("alpha", "beta")
        pf = self._pf('llm="alpha|beta"')
        runner = Runner(pf, NamedDispatcher("x"), verbose=False)
        runner.run("review")
        artifact = runner.artifacts["review"]
        assert artifact["alpha.response"] == "answer from alpha"
        assert artifact["beta.response"] == "answer from beta"

    def test_one_failure_does_not_fail_the_step(self, providers):
        providers("alpha", "beta", beta=NamedDispatcher("beta", success=False))
        pf = self._pf('llm="alpha|beta"')
        result = Runner(pf, NamedDispatcher("x"), verbose=False).run("review")
        assert result.success
        assert "answer from alpha" in result.task_results[0].response

    def test_total_failure_fails_the_step(self, providers):
        providers(
            "alpha",
            "beta",
            alpha=NamedDispatcher("alpha", success=False),
            beta=NamedDispatcher("beta", success=False),
        )
        pf = self._pf('llm="alpha|beta"')
        assert not Runner(pf, NamedDispatcher("x"), verbose=False).run("review").success

    def test_providers_run_concurrently(self, providers):
        registry = providers(
            "alpha",
            "beta",
            alpha=NamedDispatcher("alpha", delay=0.15),
            beta=NamedDispatcher("beta", delay=0.15),
        )
        pf = self._pf('llm="alpha|beta"')
        started = time.monotonic()
        Runner(pf, registry["alpha"], verbose=False).run("review")
        # Sequential would take at least 0.30s.
        assert time.monotonic() - started < 0.28

    def test_fanout_respects_provider_concurrency_cap(self, monkeypatch):
        peak = {"current": 0, "max": 0}
        lock = threading.Lock()

        class Counting(Dispatcher):
            def dispatch(self, prompt, task):
                with lock:
                    peak["current"] += 1
                    peak["max"] = max(peak["max"], peak["current"])
                time.sleep(0.05)
                with lock:
                    peak["current"] -= 1
                return DispatchResult(response="ok", success=True)

        shared = Counting()
        monkeypatch.setattr("makethlm.runner._dispatcher_for_provider", lambda provider: shared)
        pf = parse("""\
llm alpha [model=a, max-concurrency=1]
llm beta [model=b, max-concurrency=1]

task review [llm="alpha|alpha"]:
    review this
""")
        Runner(pf, shared, verbose=False).run("review")
        assert peak["max"] == 1


class TestJudge:
    def _pf(self):
        return parse(
            PROVIDER_DECLS + '\ntask review [llm="alpha|beta", judge=gamma]:\n    review this\n'
        )

    def test_judge_answer_becomes_the_response(self, providers):
        providers("alpha", "beta", "gamma")
        result = Runner(self._pf(), NamedDispatcher("x"), verbose=False).run("review")
        assert result.task_results[0].response == "merged by gamma"

    def test_judge_sees_every_answer(self, providers):
        registry = providers("alpha", "beta", "gamma")
        Runner(self._pf(), NamedDispatcher("x"), verbose=False).run("review")
        judge_prompt = registry["gamma"].prompts[-1]
        assert "answer from alpha" in judge_prompt
        assert "answer from beta" in judge_prompt

    def test_individual_answers_survive_judging(self, providers):
        providers("alpha", "beta", "gamma")
        runner = Runner(self._pf(), NamedDispatcher("x"), verbose=False)
        runner.run("review")
        assert runner.artifacts["review"]["alpha.response"] == "answer from alpha"

    def test_failed_judge_falls_back_to_all_answers(self, providers):
        providers("alpha", "beta", "gamma", gamma=NamedDispatcher("gamma", success=False))
        result = Runner(self._pf(), NamedDispatcher("x"), verbose=False).run("review")
        assert result.success
        assert "answer from alpha" in result.task_results[0].response


class TestChainExecution:
    def test_answer_pipes_into_the_next_prompt(self, providers):
        registry = providers("alpha", "beta")
        pf = parse(
            PROVIDER_DECLS
            + """
task chain:
    @llm alpha
    draft it |>
    @llm beta
    tighten it
"""
        )
        Runner(pf, NamedDispatcher("x"), verbose=False).run("chain")
        second_prompt = registry["beta"].prompts[-1]
        assert "answer from alpha" in second_prompt
        assert "tighten it" in second_prompt

    def test_pipe_context_is_labeled_by_provider(self, providers):
        registry = providers("alpha", "beta")
        pf = parse(
            PROVIDER_DECLS
            + """
task chain:
    @llm alpha
    draft it |>
    @llm beta
    tighten it
"""
        )
        Runner(pf, NamedDispatcher("x"), verbose=False).run("chain")
        assert "Answer from alpha:" in registry["beta"].prompts[-1]

    def test_without_pipe_no_context_is_carried(self, providers):
        registry = providers("alpha", "beta")
        pf = parse(
            PROVIDER_DECLS
            + """
task chain:
    @llm alpha
    draft it
    @llm beta
    tighten it
"""
        )
        Runner(pf, NamedDispatcher("x"), verbose=False).run("chain")
        assert "answer from alpha" not in registry["beta"].prompts[-1]

    def test_step_provider_overrides_task_fanout(self, providers):
        registry = providers("alpha", "beta", "gamma")
        pf = parse(
            PROVIDER_DECLS
            + """
task chain [llm="alpha|beta"]:
    @llm gamma
    just gamma
"""
        )
        Runner(pf, NamedDispatcher("x"), verbose=False).run("chain")
        assert registry["gamma"].prompts
        assert not registry["alpha"].prompts
        assert not registry["beta"].prompts


class TestPromptBuilders:
    def test_fanout_response_labels_each_answer(self):
        results = [
            ("alpha", DispatchResult(response="one", success=True)),
            ("beta", DispatchResult(response="two", success=False)),
        ]
        text = format_fanout_response(results)
        assert "[alpha]" in text
        assert "[beta (failed)]" in text

    def test_judge_prompt_includes_task_and_answers(self):
        prompt = build_judge_prompt("review this", [("alpha", "one"), ("beta", "two")])
        assert "review this" in prompt
        assert "--- answer from alpha ---" in prompt
        assert "--- answer from beta ---" in prompt
        assert "one" in prompt and "two" in prompt
