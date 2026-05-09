import pytest

from backtest.enums import MarketCategory, OrderType, Side
from backtest.market import Market
from backtest.validators import (
    OrderRejection, validate_limit, validate_market_buy, validate_market_sell,
)


def _market(tick: float | None = 0.01) -> Market:
    m = Market(
        slug="test", condition_id="c", token_ids=("A", "B"),
        outcome_names=("Yes", "No"), category=MarketCategory.SPORTS,
    )
    if tick is not None:
        m.observe_tick([tick * 5])     # any single tick-aligned price seeds it
    return m


# ── limit ──

def test_limit_happy():
    validate_limit(
        market=_market(), side=Side.BUY, size_shares=100, price=0.50,
        order_type=OrderType.GTC, post_only=False, expiration_ms=None, now_ms=0,
    )


def test_limit_off_tick_rejects():
    with pytest.raises(OrderRejection, match="not a multiple"):
        validate_limit(
            market=_market(), side=Side.BUY, size_shares=100, price=0.503,
            order_type=OrderType.GTC, post_only=False, expiration_ms=None, now_ms=0,
        )


def test_limit_outside_0_1_rejects():
    for bad in (0.0, 1.0, -0.5, 1.5):
        with pytest.raises(OrderRejection, match="strictly between 0 and 1"):
            validate_limit(
                market=_market(), side=Side.BUY, size_shares=10, price=bad,
                order_type=OrderType.GTC, post_only=False, expiration_ms=None, now_ms=0,
            )


def test_limit_negative_size_rejects():
    with pytest.raises(OrderRejection, match="positive"):
        validate_limit(
            market=_market(), side=Side.BUY, size_shares=0, price=0.5,
            order_type=OrderType.GTC, post_only=False, expiration_ms=None, now_ms=0,
        )


def test_gtd_needs_expiration():
    with pytest.raises(OrderRejection, match="GTD"):
        validate_limit(
            market=_market(), side=Side.BUY, size_shares=10, price=0.5,
            order_type=OrderType.GTD, post_only=False, expiration_ms=None, now_ms=0,
        )


def test_gtd_min_lifetime():
    with pytest.raises(OrderRejection, match="60"):
        validate_limit(
            market=_market(), side=Side.BUY, size_shares=10, price=0.5,
            order_type=OrderType.GTD, post_only=False,
            expiration_ms=30_000, now_ms=0,    # only 30s away
        )


def test_gtd_exactly_60s_ok():
    validate_limit(
        market=_market(), side=Side.BUY, size_shares=10, price=0.5,
        order_type=OrderType.GTD, post_only=False,
        expiration_ms=60_000, now_ms=0,
    )


def test_limit_with_no_observed_tick_still_passes_inside_0_1():
    # Permissive default before any book has been seen
    validate_limit(
        market=_market(tick=None), side=Side.BUY, size_shares=10, price=0.5234,
        order_type=OrderType.GTC, post_only=False, expiration_ms=None, now_ms=0,
    )


# ── market buy ──

def test_market_buy_happy():
    validate_market_buy(
        market=_market(), notional_usdc=10, order_type=OrderType.FAK,
        slip_limit_price=0.55,
    )


def test_market_buy_zero_notional_rejects():
    with pytest.raises(OrderRejection):
        validate_market_buy(
            market=_market(), notional_usdc=0, order_type=OrderType.FAK,
            slip_limit_price=None,
        )


def test_market_buy_off_tick_slip_rejects():
    with pytest.raises(OrderRejection, match="not a multiple"):
        validate_market_buy(
            market=_market(), notional_usdc=10, order_type=OrderType.FAK,
            slip_limit_price=0.555,
        )


def test_market_buy_with_gtc_rejects():
    with pytest.raises(OrderRejection, match="FAK or FOK"):
        validate_market_buy(
            market=_market(), notional_usdc=10, order_type=OrderType.GTC,
            slip_limit_price=None,
        )


# ── market sell ──

def test_market_sell_happy():
    validate_market_sell(
        market=_market(), size_shares=100, order_type=OrderType.FAK,
        slip_limit_price=0.50,
    )


def test_fok_sell_maker_too_precise():
    with pytest.raises(OrderRejection, match="size"):
        validate_market_sell(
            market=_market(), size_shares=1.745, order_type=OrderType.FOK,
            slip_limit_price=0.50,
        )


def test_fok_sell_taker_amount_too_precise():
    # Use a tick-valid price (0.5234 on 0.0001 tick) where size*price still
    # exceeds 4 dp: 0.01 × 0.5234 = 0.005234 (6 dp).
    with pytest.raises(OrderRejection, match="taker amount"):
        validate_market_sell(
            market=_market(tick=0.0001), size_shares=0.01,
            order_type=OrderType.FOK, slip_limit_price=0.5234,
        )
