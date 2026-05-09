"""Auto-collected run metrics — engine wires this in, strategies don't touch it.

On every fill: append to fill_log + score adverse selection.
On every book: sample equity (cash + unrealized).
On run end: produce a `MetricsReport`.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Optional

from .book import BookSnapshot
from .config import ADVERSE_LOOKAHEAD_MS
from .enums import Side
from .orders import Fill
from .portfolio import PortfolioSnapshot


@dataclass
class FillRecord:
    ts_ms: int
    order_id: str
    token_id: str
    side: str
    price: float
    size_shares: float
    is_maker: bool
    fee_usdc: float
    matched_against_tx: str

    @classmethod
    def from_fill(cls, f: Fill) -> "FillRecord":
        return cls(
            ts_ms=f.ts_ms, order_id=f.order_id, token_id=f.token_id,
            side=f.side.value, price=f.price, size_shares=f.size_shares,
            is_maker=f.is_maker, fee_usdc=f.fee_usdc,
            matched_against_tx=f.matched_against_tx,
        )


@dataclass
class EquitySample:
    ts_ms: int
    cash: float
    unrealized: float
    realized: float
    fees: float
    total: float


@dataclass
class MetricsReport:
    total_pnl_usdc: float
    realized_pnl_usdc: float
    unrealized_pnl_usdc: float
    fees_paid_usdc: float

    num_fills: int
    num_maker_fills: int
    num_taker_fills: int
    maker_share_pct: float

    volume_shares: float
    volume_notional_usdc: float

    sharpe_per_sample: Optional[float]
    max_drawdown_usdc: float
    adverse_selection_bps_avg: Optional[float]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class MetricsRecorder:
    """Engine-owned. Strategies do not touch this."""

    def __init__(self, adverse_window_ms: int = ADVERSE_LOOKAHEAD_MS):
        self._fills: list[FillRecord] = []
        self._equity: list[EquitySample] = []
        # (fill, ts0_ms) waiting to be scored when a future book shows up
        self._pending_adverse: list[tuple[Fill, int]] = []
        self._adverse_bps: list[float] = []
        self._adverse_window_ms = adverse_window_ms

    # ── ingest ───────────────────────────────────────────────────────

    def record_fill(self, fill: Fill) -> None:
        self._fills.append(FillRecord.from_fill(fill))
        if fill.is_maker:
            self._pending_adverse.append((fill, fill.ts_ms))

    def record_book(self, snapshot: BookSnapshot) -> None:
        mid = snapshot.mid
        if mid is None:
            return
        kept: list[tuple[Fill, int]] = []
        for fill, t0 in self._pending_adverse:
            if fill.token_id != snapshot.token_id:
                kept.append((fill, t0))
                continue
            if snapshot.ts_ms - t0 >= self._adverse_window_ms:
                # Positive bps = adverse for the maker; negative = favorable.
                if fill.side == Side.BUY:
                    bps = (fill.price - mid) / fill.price * 1e4
                else:
                    bps = (mid - fill.price) / fill.price * 1e4
                self._adverse_bps.append(bps)
            else:
                kept.append((fill, t0))
        self._pending_adverse = kept

    def record_portfolio(self, ts_ms: int, snap: PortfolioSnapshot) -> None:
        self._equity.append(EquitySample(
            ts_ms=ts_ms, cash=snap.cash_usdc,
            unrealized=snap.unrealized_pnl_usdc,
            realized=snap.realized_pnl_usdc,
            fees=snap.fees_paid_usdc,
            total=snap.total_pnl_usdc,
        ))

    # ── output ───────────────────────────────────────────────────────

    @property
    def fill_records(self) -> list[FillRecord]:
        return list(self._fills)

    @property
    def equity_curve(self) -> list[EquitySample]:
        return list(self._equity)

    def report(self) -> MetricsReport:
        n = len(self._fills)
        maker = sum(1 for f in self._fills if f.is_maker)
        volume = sum(f.size_shares for f in self._fills)
        notional = sum(f.size_shares * f.price for f in self._fills)
        fees = sum(f.fee_usdc for f in self._fills)

        if self._equity:
            last = self._equity[-1]
            realized = last.realized
            unrealized = last.unrealized
            total = last.total
        else:
            realized = unrealized = total = 0.0

        sharpe = self._sharpe([s.total for s in self._equity])
        dd = self._max_drawdown([s.total for s in self._equity])
        adv = (
            statistics.fmean(self._adverse_bps)
            if self._adverse_bps else None
        )

        return MetricsReport(
            total_pnl_usdc=total,
            realized_pnl_usdc=realized,
            unrealized_pnl_usdc=unrealized,
            fees_paid_usdc=fees,
            num_fills=n,
            num_maker_fills=maker,
            num_taker_fills=n - maker,
            maker_share_pct=(100.0 * maker / n) if n else 0.0,
            volume_shares=volume,
            volume_notional_usdc=notional,
            sharpe_per_sample=sharpe,
            max_drawdown_usdc=dd,
            adverse_selection_bps_avg=adv,
        )

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _sharpe(curve: list[float]) -> Optional[float]:
        if len(curve) < 3:
            return None
        deltas = [b - a for a, b in zip(curve, curve[1:])]
        mean = statistics.fmean(deltas)
        std = statistics.pstdev(deltas)
        if std <= 0:
            return None
        return mean / std

    @staticmethod
    def _max_drawdown(curve: list[float]) -> float:
        if not curve:
            return 0.0
        peak = curve[0]
        worst = 0.0
        for x in curve:
            if x > peak:
                peak = x
            dd = peak - x
            if dd > worst:
                worst = dd
        return worst
