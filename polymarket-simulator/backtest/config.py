"""Configuration constants — single source of truth for engine-wide tunables.

Anything tweakable lives here. No magic numbers in other modules.
"""

from __future__ import annotations

# ── Numerical tolerances ────────────────────────────────────────────
# Two prices within this distance are considered equal (handles FP noise on
# tick math). Tight enough to catch off-by-one cents, loose enough to absorb
# float arithmetic drift like 0.585 → 0.5850000000000001.
PRICE_EPS: float = 1e-9
SIZE_EPS: float = 1e-9


# ── Polymarket protocol constants ───────────────────────────────────
# All fees rounded to this many decimals; smallest charge below this is 0.
FEE_DECIMALS: int = 5
MIN_FEE_USDC: float = 1e-5

# GTD orders must expire at least this many seconds in the future.
GTD_MIN_LIFETIME_S: int = 60

# Maximum precision rules for FOK SELL (per py-clob-client issue #121).
FOK_SELL_MAKER_AMOUNT_DECIMALS: int = 2
FOK_SELL_TAKER_AMOUNT_DECIMALS: int = 4

# Polymarket tick sizes that exist; sorted from coarsest to finest.
VALID_TICKS: tuple[float, ...] = (0.1, 0.01, 0.001, 0.0001)


# Adverse selection look-ahead: how long after a maker fill we sample the
# mid to score the fill's directional drift.
ADVERSE_LOOKAHEAD_MS: int = 5_000


# ── Replay hygiene ──────────────────────────────────────────────────
# Trades whose (local_ts_ms − clob_ts_ms) exceeds this threshold are
# treated as catch-up trades (REST-polled after a WebSocket gap, or trades
# from before the capture started). They are dropped from the replay
# because we have no book state for the moment they really occurred.
MAX_TRADE_CAPTURE_LAG_MS: int = 60_000
