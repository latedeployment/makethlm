"""Tests for token/cost accounting and spend budgets."""

from __future__ import annotations

import pytest

from makethlm.cost import CostTotals, derive_cost, parse_cost
from makethlm.dispatcher import Dispatcher, DispatchResult, _claude_dispatch_result
from makethlm.models import LLMProvider
from makethlm.parser import ParseError, parse
from makethlm.runner import Runner


class UsageDispatcher(Dispatcher):
    """A provider that reports token usage on every call."""

    def __init__(self, tokens_in=1000, tokens_out=500, cost_usd=None):
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_usd = cost_usd
        self.calls = 0

    def dispatch(self, prompt, task):
        self.calls += 1
        return DispatchResult(
            response="done",
            success=True,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            cost_usd=self.cost_usd,
        )


class TestParseCost:
    def test_plain_number(self):
        assert parse_cost("2.50") == 2.5

    def test_dollar_sign(self):
        assert parse_cost("$2.50") == 2.5

    def test_rejects_text(self):
        with pytest.raises(ValueError, match="invalid cost"):
            parse_cost("cheap")

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="must not be negative"):
            parse_cost("-1")


class TestDeriveCost:
    def test_uses_declared_prices(self):
        provider = LLMProvider(name="openai", price_in=3.0, price_out=15.0)
        # 1M in at $3 + 0.5M out at $15 = $10.50
        assert derive_cost(provider, 1_000_000, 500_000) == pytest.approx(10.5)

    def test_none_without_prices(self):
        assert derive_cost(LLMProvider(name="openai"), 1000, 500) is None

    def test_none_without_usage(self):
        provider = LLMProvider(name="openai", price_in=3.0, price_out=15.0)
        assert derive_cost(provider, None, None) is None

    def test_none_provider(self):
        assert derive_cost(None, 100, 100) is None

    def test_partial_prices_are_honored(self):
        provider = LLMProvider(name="openai", price_in=2.0)
        assert derive_cost(provider, 1_000_000, 999) == pytest.approx(2.0)


class TestCostTotals:
    def test_accumulates(self):
        totals = CostTotals()
        totals.add(100, 50, 0.01)
        totals.add(200, 60, 0.02)
        assert totals.tokens_in == 300
        assert totals.tokens_out == 110
        assert totals.cost_usd == pytest.approx(0.03)
        assert totals.calls == 2

    def test_unknown_cost_is_counted_separately(self):
        totals = CostTotals()
        totals.add(100, 50, None)
        assert totals.unpriced_calls == 1
        assert totals.cost_usd == 0.0

    def test_summary_mentions_unpriced_calls(self):
        totals = CostTotals()
        totals.add(10, 5, None)
        assert "unpriced" in totals.summary()


class TestClaudeUsageParsing:
    def test_reads_json_envelope(self):
        payload = (
            '{"result": "the answer", "total_cost_usd": 0.42, '
            '"usage": {"input_tokens": 120, "output_tokens": 30}}'
        )
        result = _claude_dispatch_result(payload, True)
        assert result.response == "the answer"
        assert result.tokens_in == 120
        assert result.tokens_out == 30
        assert result.cost_usd == 0.42

    def test_plain_text_still_works(self):
        result = _claude_dispatch_result("just some text", True)
        assert result.response == "just some text"
        assert result.tokens_in is None

    def test_json_without_result_key_is_passed_through(self):
        # A task whose answer is itself JSON must not be mistaken for an envelope.
        result = _claude_dispatch_result('{"ok": true}', True)
        assert result.response == '{"ok": true}'

    def test_error_envelope_fails(self):
        result = _claude_dispatch_result('{"result": "oops", "is_error": true}', True)
        assert not result.success


@pytest.fixture
def install_dispatcher(monkeypatch):
    """Force every declared provider to resolve to the given dispatcher."""

    def _install(dispatcher):
        monkeypatch.setattr(
            "makethlm.runner._dispatcher_for_provider",
            lambda provider: dispatcher,
        )
        return dispatcher

    return _install


PF = """\
llm priced [model=x, price-in=3.0, price-out=15.0]

task review:
    review the code
"""


class TestRunnerAccounting:
    def test_tokens_and_cost_are_totalled(self, install_dispatcher):
        pf = parse(PF)
        dispatcher = install_dispatcher(UsageDispatcher(tokens_in=1_000_000, tokens_out=1_000_000))
        runner = Runner(pf, dispatcher, verbose=False)
        runner.run("review")
        assert runner.costs.tokens_in == 1_000_000
        assert runner.costs.cost_usd == pytest.approx(18.0)

    def test_provider_reported_cost_wins_over_prices(self, install_dispatcher):
        pf = parse(PF)
        runner = Runner(pf, install_dispatcher(UsageDispatcher(cost_usd=0.05)), verbose=False)
        runner.run("review")
        assert runner.costs.cost_usd == pytest.approx(0.05)

    def test_unpriced_provider_records_tokens_only(self, install_dispatcher):
        pf = parse("""\
llm plain [model=x]

task review:
    review the code
""")
        runner = Runner(pf, install_dispatcher(UsageDispatcher()), verbose=False)
        runner.run("review")
        assert runner.costs.tokens_in == 1000
        assert runner.costs.cost_usd == 0.0
        assert runner.costs.unpriced_calls == 1

    def test_replayed_fixture_costs_nothing(self, tmp_path, install_dispatcher):
        pf = parse(PF)
        record = Runner(
            pf,
            install_dispatcher(UsageDispatcher(cost_usd=1.0)),
            verbose=False,
            fixtures_dir=str(tmp_path),
            record_fixtures=True,
        )
        record.run("review")
        replay = Runner(
            parse(PF),
            install_dispatcher(UsageDispatcher(cost_usd=1.0)),
            verbose=False,
            fixtures_dir=str(tmp_path),
        )
        replay.run("review")
        assert replay.costs.cost_usd == 0.0


class TestBudgets:
    def _pf(self, extra=""):
        return parse(f"""\
llm priced [model=x, price-in=3.0, price-out=15.0]

task a{extra}:
    first

task b{extra}: a:
    second
""")

    def test_run_stops_after_budget_is_spent(self, install_dispatcher):
        # Each call costs $18; a $1 budget allows the first call, then stops.
        dispatcher = install_dispatcher(UsageDispatcher(tokens_in=1_000_000, tokens_out=1_000_000))
        runner = Runner(self._pf(), dispatcher, verbose=False, max_cost=1.0)
        result = runner.run("b")
        assert not result.success
        assert dispatcher.calls == 1
        assert "budget exceeded" in result.task_results[-1].response

    def test_run_completes_within_budget(self, install_dispatcher):
        dispatcher = install_dispatcher(UsageDispatcher(tokens_in=1000, tokens_out=1000))
        runner = Runner(self._pf(), dispatcher, verbose=False, max_cost=1.0)
        result = runner.run("b")
        assert result.success
        assert dispatcher.calls == 2

    def test_task_option_budget_applies(self, install_dispatcher):
        dispatcher = install_dispatcher(UsageDispatcher(tokens_in=1_000_000, tokens_out=1_000_000))
        runner = Runner(self._pf(' [max-cost="1.00"]'), dispatcher, verbose=False)
        result = runner.run("b")
        assert not result.success
        assert dispatcher.calls == 1

    def test_no_budget_means_no_limit(self, install_dispatcher):
        dispatcher = install_dispatcher(UsageDispatcher(tokens_in=1_000_000, tokens_out=1_000_000))
        runner = Runner(self._pf(), dispatcher, verbose=False)
        assert runner.run("b").success
        assert dispatcher.calls == 2

    def test_parser_rejects_bad_budget(self):
        with pytest.raises(ParseError, match="invalid cost"):
            parse("""\
task review [max-cost=free]:
    review
""")


class TestProviderPricing:
    def test_parses_prices(self):
        pf = parse("llm openai [model=gpt, price-in=2.5, price-out=10]\n\ntask t:\n    do it\n")
        provider = pf.llm_providers["openai"]
        assert provider.price_in == 2.5
        assert provider.price_out == 10.0

    def test_rejects_negative_price(self):
        with pytest.raises(ParseError, match="must not be negative"):
            parse("llm openai [price-in=-1]\n\ntask t:\n    do it\n")

    def test_rejects_non_numeric_price(self):
        with pytest.raises(ParseError, match="must be a number"):
            parse("llm openai [price-in=cheap]\n\ntask t:\n    do it\n")
