"""Per-market state — slug, tokens, fee category, current tick.

A `Market` is created once per replay run. The current tick is mutable: the
engine updates it from each incoming book snapshot via Market.observe_tick().
Strategies and validators read Market.tick() for the live value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .enums import MarketCategory
from .fees import rate_for_category
from .tick import infer_tick_from_prices


@dataclass
class Market:
    slug: str
    condition_id: str
    token_ids: tuple[str, str]
    outcome_names: tuple[str, str]
    category: MarketCategory = MarketCategory.SPORTS

    # Mutable state; updated by Engine on each book snapshot.
    _current_tick: Optional[float] = field(default=None, repr=False)

    def __post_init__(self):
        if len(self.token_ids) != 2:
            raise ValueError(
                f"binary market needs exactly 2 token_ids, got {self.token_ids}"
            )

    # ── tick ─────────────────────────────────────────────────────────

    def tick(self) -> Optional[float]:
        """Most recent observed tick. None until the first snapshot arrives."""
        return self._current_tick

    def observe_tick(self, prices: Iterable[float]) -> float:
        """Refine the tick from a fresh batch of visible book prices.

        Tick can only get *finer* during a match (Polymarket adds precision
        when markets become one-sided). We never coarsen mid-run since a few
        zero-padded prices in a snapshot would mask the true tick.
        """
        observed = infer_tick_from_prices(prices)
        if self._current_tick is None or observed < self._current_tick:
            self._current_tick = observed
        return self._current_tick

    # ── fees ─────────────────────────────────────────────────────────

    def default_rate(self) -> float:
        """Rate to use when a captured fee_rate_bps is not present."""
        return rate_for_category(self.category)

    # ── helpers ──────────────────────────────────────────────────────

    def name_for(self, token_id: str) -> str:
        for tid, name in zip(self.token_ids, self.outcome_names):
            if tid == token_id:
                return name
        return token_id[:12]

    def other_token(self, token_id: str) -> str:
        if token_id == self.token_ids[0]:
            return self.token_ids[1]
        if token_id == self.token_ids[1]:
            return self.token_ids[0]
        raise KeyError(f"unknown token_id {token_id} for market {self.slug}")
