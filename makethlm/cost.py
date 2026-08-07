"""Token and spend accounting for LLM steps.

Providers report usage in their own shapes; this module turns that into a
single running total and enforces the budget that stops a run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import LLMProvider

TOKENS_PER_PRICE_UNIT = 1_000_000


class BudgetExceeded(Exception):
    """Raised when a run has spent more than its allowed budget."""


def parse_cost(value: str) -> float:
    """Parse a budget such as ``2.50`` or ``$2.50`` into USD."""
    text = value.strip().lstrip("$").strip()
    try:
        cost = float(text)
    except ValueError:
        raise ValueError(f"invalid cost: {value!r} (expected a number of US dollars)")
    if cost < 0:
        raise ValueError(f"cost must not be negative: {value!r}")
    return cost


def derive_cost(
    provider: LLMProvider | None,
    tokens_in: int | None,
    tokens_out: int | None,
) -> float | None:
    """Return the USD cost implied by a provider's declared prices.

    Returns ``None`` when the provider declares no prices or the call reported
    no usage, so unknown spend is never silently counted as zero.
    """
    if provider is None:
        return None
    if provider.price_in is None and provider.price_out is None:
        return None
    if tokens_in is None and tokens_out is None:
        return None
    cost = 0.0
    if provider.price_in is not None and tokens_in:
        cost += (tokens_in / TOKENS_PER_PRICE_UNIT) * provider.price_in
    if provider.price_out is not None and tokens_out:
        cost += (tokens_out / TOKENS_PER_PRICE_UNIT) * provider.price_out
    return cost


@dataclass
class CostTotals:
    """Running totals for a single run."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    unpriced_calls: int = 0  # LLM calls whose spend could not be determined

    def add(
        self,
        tokens_in: int | None,
        tokens_out: int | None,
        cost_usd: float | None,
    ) -> None:
        self.calls += 1
        self.tokens_in += tokens_in or 0
        self.tokens_out += tokens_out or 0
        if cost_usd is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += cost_usd

    @property
    def has_data(self) -> bool:
        return bool(self.tokens_in or self.tokens_out or self.cost_usd)

    def as_dict(self) -> dict[str, object]:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "unpriced_calls": self.unpriced_calls,
        }

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        parts = [f"{self.calls} LLM call{'s' if self.calls != 1 else ''}"]
        if self.tokens_in or self.tokens_out:
            parts.append(f"{self.tokens_in:,} in / {self.tokens_out:,} out tokens")
        if self.cost_usd:
            parts.append(f"${self.cost_usd:.4f}")
        if self.unpriced_calls:
            parts.append(f"{self.unpriced_calls} unpriced")
        return ", ".join(parts)
