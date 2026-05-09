"""Polymarket fee formula verified against the published Sports table."""
import pytest

from backtest.enums import MarketCategory
from backtest.fees import (
    compute_taker_fee,
    rate_for_category,
    rate_from_captured_bps,
    rate_from_v2_fee_info,
)


SPORTS = 0.03


@pytest.mark.parametrize("price,expected", [
    (0.01, 0.03),   # boundary low
    (0.05, 0.14),
    (0.10, 0.27),
    (0.25, 0.56),
    (0.50, 0.75),   # peak
    (0.55, 0.74),   # symmetric peak
    (0.75, 0.56),
    (0.90, 0.27),
    (0.95, 0.14),
    (0.99, 0.03),
])
def test_published_sports_table(price, expected):
    """Each row is 100 shares × rate × p × (1-p), rounded as Polymarket does."""
    fee = compute_taker_fee(shares=100, price=price, rate=SPORTS)
    assert round(fee, 2) == pytest.approx(expected, abs=0.01)


def test_zero_at_boundaries():
    assert compute_taker_fee(shares=100, price=0, rate=SPORTS) == 0
    assert compute_taker_fee(shares=100, price=1, rate=SPORTS) == 0


def test_zero_for_zero_rate():
    assert compute_taker_fee(shares=100, price=0.5, rate=0) == 0


def test_below_min_returns_zero():
    # 0.00001 shares × 0.03 × 0.25 = 7.5e-8, rounds below 1e-5 minimum
    assert compute_taker_fee(shares=0.00001, price=0.5, rate=SPORTS) == 0


def test_rate_for_category_sports():
    assert rate_for_category(MarketCategory.SPORTS) == SPORTS


def test_rate_from_captured_bps_present():
    assert rate_from_captured_bps(300) == 0.03


def test_rate_from_captured_bps_string():
    assert rate_from_captured_bps("300") == 0.03


def test_rate_from_captured_bps_missing():
    assert rate_from_captured_bps(None) is None
    assert rate_from_captured_bps("") is None
    assert rate_from_captured_bps("None") is None


# ── V2 (CLOB V2) fee_info helpers ────────────────────────────────────────────

def test_rate_from_v2_fee_info_passes_through_rate():
    # fd.r = 0.02 → effective rate 0.02
    assert rate_from_v2_fee_info(0.02, 0.0) == 0.02


def test_rate_from_v2_fee_info_ignores_exponent_for_now():
    # exponent has no published semantics yet — must be ignored, not crash.
    assert rate_from_v2_fee_info(0.03, 4.0) == 0.03


def test_rate_from_v2_fee_info_missing_returns_none():
    assert rate_from_v2_fee_info(None) is None
    assert rate_from_v2_fee_info(0.0) is None
    assert rate_from_v2_fee_info(-0.01) is None


def test_v2_rate_drives_compute_taker_fee():
    # End-to-end: pull rate from V2 fee_info dict shape, feed compute_taker_fee.
    rate = rate_from_v2_fee_info(0.03, 0.0)
    fee = compute_taker_fee(shares=100, price=0.5, rate=rate)
    # Same as the published Sports peak: 100 × 0.03 × 0.5 × 0.5 = 0.75
    assert round(fee, 2) == 0.75


def test_v2_fee_zero_at_extremes():
    rate = rate_from_v2_fee_info(0.05, 0.0)
    assert compute_taker_fee(shares=100, price=0.0, rate=rate) == 0
    assert compute_taker_fee(shares=100, price=1.0, rate=rate) == 0
    # At p=0.5, fee is max for given rate.
    peak = compute_taker_fee(shares=100, price=0.5, rate=rate)
    near_peak = compute_taker_fee(shares=100, price=0.45, rate=rate)
    assert peak > near_peak
