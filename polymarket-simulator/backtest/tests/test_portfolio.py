import pytest

from backtest.enums import Side
from backtest.orders import Fill
from backtest.portfolio import Portfolio


def _fill(side, price, size, is_maker=True, fee=0.0, token="T"):
    return Fill(
        order_id="o", token_id=token, side=Side(side),
        price=price, size_shares=size, ts_ms=0,
        is_maker=is_maker, fee_usdc=fee,
    )


def test_buy_increases_position_decreases_cash():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.5, 100))
    assert p.cash_usdc == pytest.approx(950)
    assert p.position("T") == 100


def test_sell_realizes_pnl_fifo():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.40, 100))
    p.apply(_fill("BUY", 0.50, 100))
    p.apply(_fill("SELL", 0.55, 150))   # 100@0.40 fully + 50@0.50
    assert p.realized_pnl_usdc == pytest.approx(15.0 + 2.5)
    assert p.position("T") == pytest.approx(50)


def test_taker_fee_deducted():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.5, 100, is_maker=False, fee=0.75))
    assert p.cash_usdc == pytest.approx(949.25)
    assert p.fees_paid_usdc == pytest.approx(0.75)


def test_maker_fill_no_fee():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.5, 100, is_maker=True, fee=0.0))
    assert p.fees_paid_usdc == 0


def test_oversell_raises():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.5, 10))
    with pytest.raises(RuntimeError, match="insufficient inventory"):
        p.apply(_fill("SELL", 0.5, 100))


def test_set_initial_position_seeds_lots():
    p = Portfolio(1000)
    p.set_initial_position("T", 100, avg_cost=0.4)
    assert p.position("T") == 100
    p.apply(_fill("SELL", 0.6, 100))    # realize against seed
    assert p.realized_pnl_usdc == pytest.approx(20.0)


def test_snapshot_marks_unrealized():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.40, 100))
    snap = p.snapshot(marks={"T": 0.60})
    assert snap.unrealized_pnl_usdc == pytest.approx(20)
    assert snap.total_pnl_usdc == pytest.approx(20)


def test_positions_filters_zero():
    p = Portfolio(1000)
    p.apply(_fill("BUY", 0.5, 10, token="A"))
    p.apply(_fill("SELL", 0.6, 10, token="A"))
    p.apply(_fill("BUY", 0.5, 5, token="B"))
    assert set(p.positions().keys()) == {"B"}
