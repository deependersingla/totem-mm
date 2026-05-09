"""FIFO-costed portfolio.

Public surface only — no `_positions` mutation from outside. Use
`set_initial_position` for seeded tests.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Mapping

from .config import SIZE_EPS
from .enums import Side
from .orders import Fill


@dataclass
class _Lot:
    shares: float
    cost_per_share: float


@dataclass
class PortfolioSnapshot:
    cash_usdc: float
    realized_pnl_usdc: float
    fees_paid_usdc: float
    positions: dict[str, float]
    unrealized_pnl_usdc: float
    total_pnl_usdc: float


class Portfolio:
    def __init__(self, starting_cash_usdc: float):
        self.cash_usdc: float = float(starting_cash_usdc)
        self.realized_pnl_usdc: float = 0.0
        self.fees_paid_usdc: float = 0.0
        self._lots: dict[str, deque[_Lot]] = defaultdict(deque)
        self._positions: dict[str, float] = defaultdict(float)

    # ── public seeding (used by validator and tests) ─────────────────

    def set_initial_position(
        self, token_id: str, shares: float, avg_cost: float = 0.0
    ) -> None:
        """Seed a position without affecting cash/realized/fees.

        Useful for replaying a wallet that started the capture with existing
        inventory, or for MM strategies that pre-seed.
        """
        if shares < 0:
            raise ValueError("shares must be non-negative")
        self._lots[token_id].clear()
        self._positions[token_id] = shares
        if shares > 0:
            self._lots[token_id].append(_Lot(shares=shares, cost_per_share=avg_cost))

    # ── core ─────────────────────────────────────────────────────────

    def apply(self, fill: Fill) -> None:
        notional = fill.size_shares * fill.price
        if fill.side == Side.BUY:
            self.cash_usdc -= notional
            self._lots[fill.token_id].append(
                _Lot(shares=fill.size_shares, cost_per_share=fill.price)
            )
            self._positions[fill.token_id] += fill.size_shares
        else:
            self.cash_usdc += notional
            self._positions[fill.token_id] -= fill.size_shares
            self._consume_lots(fill.token_id, fill.size_shares, fill.price)

        if not fill.is_maker:
            self.cash_usdc -= fill.fee_usdc
            self.fees_paid_usdc += fill.fee_usdc

    def _consume_lots(self, token_id: str, shares: float, sell_price: float) -> None:
        remaining = shares
        lots = self._lots[token_id]
        while remaining > SIZE_EPS and lots:
            lot = lots[0]
            take = min(lot.shares, remaining)
            self.realized_pnl_usdc += (sell_price - lot.cost_per_share) * take
            lot.shares -= take
            remaining -= take
            if lot.shares <= SIZE_EPS:
                lots.popleft()
        if remaining > SIZE_EPS:
            raise RuntimeError(
                f"insufficient inventory of {token_id}: short by {remaining} shares"
            )

    # ── queries ──────────────────────────────────────────────────────

    def position(self, token_id: str) -> float:
        return self._positions.get(token_id, 0.0)

    def positions(self) -> dict[str, float]:
        return {t: s for t, s in self._positions.items() if abs(s) > SIZE_EPS}

    def avg_cost(self, token_id: str) -> float:
        lots = self._lots.get(token_id)
        if not lots:
            return 0.0
        total = sum(l.shares for l in lots)
        if total <= 0:
            return 0.0
        return sum(l.shares * l.cost_per_share for l in lots) / total

    def snapshot(self, marks: Mapping[str, float]) -> PortfolioSnapshot:
        unrealized = 0.0
        for token_id, shares in self._positions.items():
            if abs(shares) < SIZE_EPS:
                continue
            mark = marks.get(token_id)
            if mark is None:
                continue
            unrealized += (mark - self.avg_cost(token_id)) * shares

        return PortfolioSnapshot(
            cash_usdc=self.cash_usdc,
            realized_pnl_usdc=self.realized_pnl_usdc,
            fees_paid_usdc=self.fees_paid_usdc,
            positions=self.positions(),
            unrealized_pnl_usdc=unrealized,
            total_pnl_usdc=self.realized_pnl_usdc - self.fees_paid_usdc + unrealized,
        )
