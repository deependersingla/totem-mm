"""Polymarket taker fee — exact formula from docs.polymarket.com/trading/fees.

    fee_usdc = shares × rate × price × (1 − price)

Verified against the published Sports table (Apr 2026), every row matches.
Maker fees are zero. Rebates are not modeled.

CLOB V2 (post 2026-04-28) sources `rate` per-market from
`GET /clob-markets/{condition_id}` (`fd.r` = rate, `fd.e` = exponent).
The formula shape is unchanged. `fd.e` is captured but not yet used in
math — when Polymarket publishes the exponent semantics, fold it in.

If a captured trade snapshot has a per-trade rate, use that instead of the
category default — it is the authoritative rate Polymarket charged.
"""

from __future__ import annotations

from .config import FEE_DECIMALS, MIN_FEE_USDC
from .enums import MarketCategory


# Category default rates (unitless, see the formula). Used when no V2
# per-market `fd.r` is available — e.g. replaying old captures.
DEFAULT_RATES: dict[MarketCategory, float] = {
    MarketCategory.SPORTS:    0.03,
    MarketCategory.POLITICS:  0.04,
    MarketCategory.FINANCE:   0.04,
    MarketCategory.CRYPTO:    0.072,
    MarketCategory.ECONOMICS: 0.05,
    MarketCategory.OTHER:     0.05,
}


def compute_taker_fee(
    *,
    shares: float,
    price: float,
    rate: float,
) -> float:
    """Return the USDC fee for a taker fill of `shares` at `price`.

    Returns 0 for zero-rate categories or near-boundary prices (rounded
    below MIN_FEE_USDC).
    """
    if rate <= 0 or shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = shares * rate * price * (1.0 - price)
    rounded = round(raw, FEE_DECIMALS)
    return rounded if rounded >= MIN_FEE_USDC else 0.0


def rate_for_category(category: MarketCategory) -> float:
    return DEFAULT_RATES.get(category, DEFAULT_RATES[MarketCategory.OTHER])


def rate_from_captured_bps(captured_bps: int | str | None) -> float | None:
    """Convert a V1-style captured fee_rate_bps value to a unitless rate.

    Captured values come from the V1 CLOB feed and may be ints or strings.
    Returns None when missing/empty so caller can fall back to the
    category default.
    """
    if captured_bps in (None, "", "None"):
        return None
    try:
        bps = int(captured_bps)
    except (TypeError, ValueError):
        return None
    return bps / 10_000.0


def rate_from_v2_fee_info(
    rate: float | None,
    exponent: float | None = None,
) -> float | None:
    """Convert V2 `/clob-markets/{condition_id}` fee fields to an effective rate.

    V2 returns `fd.r` (rate, already unitless float) and `fd.e` (exponent).
    The formula `fee = C × rate × p × (1 − p)` uses C=1 implicitly today —
    no published spec ties C to the exponent yet. Until the spec arrives,
    return `rate` and ignore `exponent`. When `exponent != 0` is observed
    on a live market, re-evaluate this helper.

    Returns None when rate is missing or non-positive, so callers can fall
    back to a category default.
    """
    if rate is None or rate <= 0:
        return None
    # Defensive: silently keep exponent unused. Caller should log it.
    _ = exponent
    return float(rate)
