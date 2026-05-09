"""End-to-end: build synthetic event streams + run real strategies. No DB."""
from __future__ import annotations

import pytest

from backtest.book import BookSnapshot, PriceLevel
from backtest.engine import Engine
from backtest.enums import MarketCategory, OrderType, Side
from backtest.events import BookEvent, CricketEvent, TradeEvent
from backtest.market import Market
from backtest.strategy import (
    ScheduledOrder, ScheduledStrategy, Strategy, StrategyContext,
)


def _market() -> Market:
    return Market(
        slug="t", condition_id="c", token_ids=("T", "U"),
        outcome_names=("Yes", "No"), category=MarketCategory.SPORTS,
    )


def _book_evt(token="T", ts=1000, bids=((0.50, 100),), asks=((0.51, 80),)) -> BookEvent:
    return BookEvent(snapshot=BookSnapshot(
        token_id=token, ts_ms=ts,
        bids=tuple(PriceLevel(p, s) for p, s in bids),
        asks=tuple(PriceLevel(p, s) for p, s in asks),
    ))


# ── Reactive strategy with synthetic events ──────────────────────────


class _RecordingStrategy(Strategy):
    def __init__(self, fak_on_first_book: bool = False):
        self.fak_on_first_book = fak_on_first_book
        self.fills = []
        self.acks = []
        self.rejects = []
        self.cancels = []
        self._submitted = False

    def on_book(self, evt, ctx):
        if self.fak_on_first_book and not self._submitted:
            self._submitted = True
            r = ctx.submit_market_buy(
                token_id="T", notional_usdc=20.4,    # 80 shares * 0.51 = 40.8 → can buy <80 with 20.4
                order_type=OrderType.FAK, slip_limit_price=0.52,
            )
            assert not r.rejected
            assert r.fills, "should fill against the synthetic book"

    def on_fill(self, evt, ctx):
        self.fills.append(evt.fill)

    def on_ack(self, evt, ctx):
        self.acks.append(evt.order_id)

    def on_reject(self, evt, ctx):
        self.rejects.append(evt.reason)

    def on_cancel(self, evt, ctx):
        self.cancels.append(evt.reason)


def test_reactive_market_buy_synchronous_result():
    engine = Engine(_market(), starting_cash_usdc=1000)
    strat = _RecordingStrategy(fak_on_first_book=True)
    engine.register(strat)
    engine.run([_book_evt()])

    assert len(strat.fills) >= 1
    assert strat.fills[0].is_maker is False
    assert strat.fills[0].fee_usdc > 0
    assert len(strat.acks) == 1


def test_resting_buy_filled_by_real_trade():
    engine = Engine(_market(), starting_cash_usdc=1000)

    class Rester(Strategy):
        def __init__(self):
            self.fills = []
            self._posted = False

        def on_book(self, evt, ctx):
            if not self._posted:
                ctx.submit_limit(
                    token_id="T", side=Side.BUY,
                    size_shares=20, price=0.50,
                    order_type=OrderType.GTC, post_only=False,
                )
                self._posted = True

        def on_fill(self, evt, ctx):
            self.fills.append(evt.fill)

    strat = Rester()
    engine.register(strat)
    engine.run([
        _book_evt(bids=((0.50, 30),), asks=((0.55, 50),)),
        TradeEvent(token_id="T", ts_ms=1100, side=Side.SELL,
                   price=0.50, size_shares=50,
                   taker_wallet="X", tx_hash="h"),
    ])
    assert len(strat.fills) == 1
    assert strat.fills[0].size_shares == 20
    assert strat.fills[0].is_maker is True
    assert strat.fills[0].fee_usdc == 0


def test_metrics_auto_collected():
    engine = Engine(_market(), starting_cash_usdc=1000)
    engine.register(_RecordingStrategy(fak_on_first_book=True))
    engine.run([_book_evt()])
    report = engine.metrics.report()
    assert report.num_fills >= 1
    assert report.num_taker_fills >= 1
    assert report.fees_paid_usdc > 0


# ── ScheduledStrategy ────────────────────────────────────────────────


def test_scheduled_strategy_submits_at_right_tick():
    engine = Engine(_market(), starting_cash_usdc=1000)
    schedule = [
        ScheduledOrder(
            ts_ms=1100, token_id="T", kind="market_buy",
            notional_usdc=10, order_type=OrderType.FAK,
            slip_limit_price=0.55,
        ),
    ]
    strat = ScheduledStrategy(schedule)
    engine.register(strat)
    engine.run([_book_evt(ts=1000), _book_evt(ts=1200)])

    report = engine.metrics.report()
    assert report.num_fills == 1
    assert report.num_taker_fills == 1
