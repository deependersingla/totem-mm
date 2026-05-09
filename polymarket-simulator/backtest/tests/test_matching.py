"""Matching engine — full path coverage using only public APIs."""
from __future__ import annotations

import pytest

from backtest.book import Book, BookSnapshot, PriceLevel
from backtest.enums import MarketCategory, OrderStatus, OrderType, Side
from backtest.events import TradeEvent
from backtest.market import Market
from backtest.matching import MatchingEngine
from backtest.orders import LimitOrder, MarketBuyOrder, MarketSellOrder


def _market() -> Market:
    m = Market(
        slug="t", condition_id="c", token_ids=("T", "U"),
        outcome_names=("Yes", "No"), category=MarketCategory.SPORTS,
    )
    m.observe_tick([0.50])  # tick 0.01
    return m


def _book(bids=((0.50, 100), (0.49, 50)), asks=((0.51, 80), (0.52, 200)),
          token="T") -> Book:
    snap = BookSnapshot(
        token_id=token, ts_ms=1000,
        bids=tuple(PriceLevel(p, s) for p, s in bids),
        asks=tuple(PriceLevel(p, s) for p, s in asks),
    )
    b = Book(token)
    b.apply(snap)
    return b


def _limit(side, size, price, ot=OrderType.GTC, post_only=False, oid="o1", token="T"):
    return LimitOrder(
        id=oid, token_id=token, side=Side(side), size_shares=size, price=price,
        order_type=ot, post_only=post_only,
    )


# ── FAK / FOK ────────────────────────────────────────────────────────


def test_fak_buy_partial_fill():
    m = MatchingEngine(_market())
    book = _book()
    order = MarketSellOrder(id="o1", token_id="T", size_shares=200,
                            order_type=OrderType.FAK, slip_limit_price=0.49)
    res = m.submit_market_sell(order=order, book=book, now_ms=1000)
    # We're selling 200 at limit 0.49 — bids: 0.50/100 and 0.49/50 → fill 150.
    assert sum(f.size_shares for f in res.fills) == pytest.approx(150)
    assert res.order.status == OrderStatus.CANCELED


def test_fak_buy_sweeps_levels():
    m = MatchingEngine(_market())
    book = _book()
    # Plenty of budget — should sweep both levels and stop at book exhaustion
    order = MarketBuyOrder(id="o1", token_id="T", notional_usdc=500,
                           order_type=OrderType.FAK, slip_limit_price=0.52)
    res = m.submit_market_buy(order=order, book=book, now_ms=1000)
    # 80 shares × 0.51 + 200 shares × 0.52 = 40.80 + 104 = 144.80
    sizes = [round(f.size_shares, 4) for f in res.fills]
    assert sizes == [80.0, 200.0]
    spent = sum(f.size_shares * f.price for f in res.fills)
    assert spent == pytest.approx(144.80, abs=1e-6)


def test_fok_buy_rejects_insufficient_notional():
    m = MatchingEngine(_market())
    book = _book(asks=((0.51, 10),))   # only $5.10 of liquidity
    order = MarketBuyOrder(id="o1", token_id="T", notional_usdc=100,
                           order_type=OrderType.FOK, slip_limit_price=0.51)
    res = m.submit_market_buy(order=order, book=book, now_ms=1000)
    assert res.fills == []
    assert res.order.status == OrderStatus.CANCELED


def test_fok_sell_fills_when_sufficient():
    m = MatchingEngine(_market())
    book = _book()
    order = MarketSellOrder(id="o1", token_id="T", size_shares=100,
                            order_type=OrderType.FOK, slip_limit_price=0.49)
    res = m.submit_market_sell(order=order, book=book, now_ms=1000)
    assert res.order.status == OrderStatus.MATCHED
    assert sum(f.size_shares for f in res.fills) == pytest.approx(100)


# ── GTC / GTD ────────────────────────────────────────────────────────


def test_gtc_passive_rests():
    m = MatchingEngine(_market())
    book = _book()
    res = m.submit_limit(order=_limit("BUY", 100, 0.49), book=book, now_ms=1000)
    assert res.fills == []
    assert res.order.status == OrderStatus.LIVE
    assert len(m.open_orders()) == 1


def test_gtc_aggressive_portion_then_rests():
    m = MatchingEngine(_market())
    book = _book()
    # BUY 300 @ 0.52: sweeps 80@0.51 + 200@0.52 = 280, 20 rests at 0.52
    res = m.submit_limit(order=_limit("BUY", 300, 0.52), book=book, now_ms=1000)
    assert sum(f.size_shares for f in res.fills) == pytest.approx(280)
    assert res.order.status == OrderStatus.PARTIALLY_FILLED


def test_post_only_rejects_cross():
    m = MatchingEngine(_market())
    book = _book()
    res = m.submit_limit(
        order=_limit("BUY", 10, 0.51, post_only=True), book=book, now_ms=1000,
    )
    assert res.rejected
    assert res.reason == "post_only_would_cross"


def test_post_only_passes_when_no_cross():
    m = MatchingEngine(_market())
    book = _book()
    res = m.submit_limit(
        order=_limit("BUY", 10, 0.49, post_only=True), book=book, now_ms=1000,
    )
    assert not res.rejected
    assert res.order.status == OrderStatus.LIVE


# ── Self-trade prevention ────────────────────────────────────────────


def test_stp_blocks_crossing_limit():
    m = MatchingEngine(_market())
    book = _book()
    m.submit_limit(order=_limit("BUY", 50, 0.50, oid="A"), book=book, now_ms=1000)
    res = m.submit_limit(order=_limit("SELL", 50, 0.50, oid="B"), book=book, now_ms=1001)
    assert res.rejected
    assert res.reason == "self_trade_prevention"


def test_stp_blocks_market_buy_with_resting_sell():
    m = MatchingEngine(_market())
    book = _book()
    m.submit_limit(order=_limit("SELL", 50, 0.55, oid="A"), book=book, now_ms=1000)
    order = MarketBuyOrder(id="B", token_id="T", notional_usdc=10,
                           order_type=OrderType.FAK)
    res = m.submit_market_buy(order=order, book=book, now_ms=1001)
    assert res.rejected
    assert res.reason == "self_trade_prevention"


# ── Real-trade matching ──────────────────────────────────────────────


def test_real_trade_fifo_consumes_queue_then_fills():
    m = MatchingEngine(_market())
    book = _book(bids=((0.50, 100),))
    m.submit_limit(order=_limit("BUY", 50, 0.50), book=book, now_ms=1000)

    trade = TradeEvent(token_id="T", ts_ms=1500, side=Side.SELL,
                       price=0.50, size_shares=80, taker_wallet="X", tx_hash="h1")
    assert m.on_real_trade(trade) == []   # 80 < queue_ahead=100

    trade2 = TradeEvent(token_id="T", ts_ms=1600, side=Side.SELL,
                        price=0.50, size_shares=50, taker_wallet="X", tx_hash="h2")
    fills = m.on_real_trade(trade2)
    assert len(fills) == 1
    assert fills[0].size_shares == 30        # 20 queue ahead + 30 to us
    assert fills[0].is_maker is True
    assert fills[0].fee_usdc == 0.0


def test_real_trade_through_fills_at_our_price():
    m = MatchingEngine(_market())
    book = _book(bids=((0.48, 100),))
    m.submit_limit(order=_limit("BUY", 40, 0.50), book=book, now_ms=1000)

    trade = TradeEvent(token_id="T", ts_ms=1500, side=Side.SELL,
                       price=0.48, size_shares=30, taker_wallet="X", tx_hash="h")
    fills = m.on_real_trade(trade)
    assert len(fills) == 1
    assert fills[0].price == 0.50            # our limit, not trade's 0.48
    assert fills[0].size_shares == 30


def test_real_trade_worse_no_fill():
    m = MatchingEngine(_market())
    book = _book(bids=((0.50, 100),))
    m.submit_limit(order=_limit("BUY", 50, 0.45), book=book, now_ms=1000)
    trade = TradeEvent(token_id="T", ts_ms=1500, side=Side.SELL,
                       price=0.50, size_shares=80, taker_wallet="X", tx_hash="h")
    assert m.on_real_trade(trade) == []


def test_real_trade_wrong_side_ignored():
    m = MatchingEngine(_market())
    book = _book()
    m.submit_limit(order=_limit("BUY", 50, 0.50), book=book, now_ms=1000)
    trade = TradeEvent(token_id="T", ts_ms=1500, side=Side.BUY,
                       price=0.51, size_shares=100, taker_wallet="X", tx_hash="h")
    assert m.on_real_trade(trade) == []


# ── Book updates reconcile queue ─────────────────────────────────────


def test_queue_advances_when_level_shrinks():
    m = MatchingEngine(_market())
    book = _book(bids=((0.50, 100),))
    m.submit_limit(order=_limit("BUY", 50, 0.50), book=book, now_ms=1000)

    new_snap = BookSnapshot(
        token_id="T", ts_ms=1500,
        bids=(PriceLevel(0.50, 40),), asks=(),
    )
    m.on_book_update(new_snap)

    # Queue ahead must have shrunk to <= 40
    entry = m.open_orders()[0]
    # Verify by feeding a trade — only 40 ahead should consume, then we fill
    trade = TradeEvent(token_id="T", ts_ms=1600, side=Side.SELL,
                       price=0.50, size_shares=70, taker_wallet="X", tx_hash="h")
    fills = m.on_real_trade(trade)
    assert len(fills) == 1
    assert fills[0].size_shares == 30


# ── Cancel / expiration ──────────────────────────────────────────────


def test_cancel_removes_order():
    m = MatchingEngine(_market())
    book = _book()
    res = m.submit_limit(order=_limit("BUY", 10, 0.49), book=book, now_ms=1000)
    cancelled = m.cancel(res.order_id, now_ms=1500)
    assert cancelled is not None
    assert cancelled.status == OrderStatus.CANCELED
    assert m.open_orders() == []


def test_gtd_expiration():
    m = MatchingEngine(_market())
    book = _book()
    o = _limit("BUY", 10, 0.49, ot=OrderType.GTD)
    o.expiration_ms = 2000
    m.submit_limit(order=o, book=book, now_ms=1000)
    assert m.expire_due(1500) == []
    expired = m.expire_due(2100)
    assert len(expired) == 1
