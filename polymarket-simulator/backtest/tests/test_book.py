import pytest

from backtest.book import Book, BookSnapshot, PriceLevel
from backtest.enums import Side


def _snap(token="T", ts=1000, bids=((0.50, 100), (0.49, 50)),
          asks=((0.51, 80), (0.52, 200))) -> BookSnapshot:
    return BookSnapshot(
        token_id=token, ts_ms=ts,
        bids=tuple(PriceLevel(p, s) for p, s in bids),
        asks=tuple(PriceLevel(p, s) for p, s in asks),
    )


def test_apply_sets_state():
    b = Book("T")
    b.apply(_snap())
    assert b.best_bid == 0.50
    assert b.best_ask == 0.51
    assert b.mid == 0.505


def test_apply_token_mismatch_rejected():
    b = Book("T")
    with pytest.raises(ValueError):
        b.apply(_snap(token="OTHER"))


def test_size_at_exact_match():
    b = Book("T")
    b.apply(_snap())
    assert b.size_at(0.50, Side.BUY) == 100
    assert b.size_at(0.52, Side.SELL) == 200
    assert b.size_at(0.30, Side.BUY) == 0


def test_crossable_for_buy_respects_limit():
    b = Book("T")
    b.apply(_snap())
    assert [lv.price for lv in b.crossable_for(Side.BUY, 0.51)] == [0.51]
    assert [lv.price for lv in b.crossable_for(Side.BUY, 0.52)] == [0.51, 0.52]


def test_crossable_for_sell():
    b = Book("T")
    b.apply(_snap())
    assert [lv.price for lv in b.crossable_for(Side.SELL, 0.50)] == [0.50]


def test_crossable_no_limit_returns_all():
    b = Book("T")
    b.apply(_snap())
    assert len(b.crossable_for(Side.BUY, None)) == 2


def test_mid_with_one_side_only():
    b = Book("T")
    b.apply(_snap(asks=()))
    assert b.mid == 0.50


def test_all_prices_iterates_all_levels():
    b = Book("T")
    b.apply(_snap())
    prices = list(b.snapshot.all_prices())
    assert sorted(prices) == [0.49, 0.50, 0.51, 0.52]
