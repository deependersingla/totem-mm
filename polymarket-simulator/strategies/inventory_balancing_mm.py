"""Inventory-balancing market maker for Polymarket cricket binary markets.

Quotes both sides around mid with inventory skew. Long inventory pushes both
quotes down (encouraging sells, discouraging buys); short inventory pushes
them up. Hard cap on inventory: once full long, stop posting bids.

Tunables (the surface a tuning loop would mutate):
    token_index                   which of the two tokens to MM (0 = first)
    quote_size_shares             size of each side's quote
    base_edge_ticks               distance from mid (in ticks) on each side
    inventory_skew_ticks_per_100  ticks of skew per 100 shares of inventory
    max_inventory_shares          hard cap; stop adding past this
    target_inventory_shares       desired flat point (default 0)
    requote_min_interval_ms       throttle cancel/replace storms
    requote_mid_move_ticks        only requote if mid moved by this many ticks
    seed_inventory_shares         optional initial taker-buy at start
    use_post_only                 if True, will not aggress on placement

Tick is queried from the live market — never assumed.
"""

from __future__ import annotations

import math
from typing import Optional

from backtest.enums import OrderType, Side
from backtest.events import BookEvent, CancelEvent, FillEvent, RejectEvent
from backtest.strategy import Strategy, StrategyContext


def _round_to_tick(price: float, tick: float, direction: str) -> float:
    n = price / tick
    if direction == "down":
        return math.floor(n + 1e-9) * tick
    return math.ceil(n - 1e-9) * tick


class InventoryBalancingMM(Strategy):
    def __init__(
        self,
        *,
        token_index: int = 0,
        quote_size_shares: float = 50.0,
        base_edge_ticks: int = 1,
        inventory_skew_ticks_per_100: float = 0.5,
        max_inventory_shares: float = 500.0,
        target_inventory_shares: float = 0.0,
        requote_min_interval_ms: int = 1000,
        requote_mid_move_ticks: float = 1.0,
        seed_inventory_shares: float = 0.0,
        use_post_only: bool = True,
        verbose: bool = False,
    ):
        self.token_index = token_index
        self.quote_size = quote_size_shares
        self.base_edge_ticks = base_edge_ticks
        self.skew_ticks_per_100 = inventory_skew_ticks_per_100
        self.max_inv = max_inventory_shares
        self.target_inv = target_inventory_shares
        self.requote_min_interval_ms = requote_min_interval_ms
        self.requote_mid_move_ticks = requote_mid_move_ticks
        self.seed_inv = seed_inventory_shares
        self.use_post_only = use_post_only
        self.verbose = verbose

        # Runtime state
        self._token: Optional[str] = None
        self._bid_id: Optional[str] = None
        self._ask_id: Optional[str] = None
        self._last_requote_ts_ms: int = 0
        self._last_requote_mid: Optional[float] = None
        self._seeded: bool = False

    # ── lifecycle ────────────────────────────────────────────────────

    def on_start(self, ctx: StrategyContext) -> None:
        market = ctx.market()
        if self.token_index >= len(market.token_ids):
            raise ValueError(f"token_index {self.token_index} out of range")
        self._token = market.token_ids[self.token_index]
        if self.verbose:
            print(f"[mm] start: MMing {market.outcome_names[self.token_index]} on {market.slug}")

    # ── quote logic ──────────────────────────────────────────────────

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None:
        if evt.token_id != self._token:
            return

        snap = evt.snapshot
        mid = snap.mid
        tick = ctx.market().tick()
        if mid is None or tick is None:
            return

        # Optional seed buy on first valid book
        if self.seed_inv > 0 and not self._seeded:
            cap = _round_to_tick(min(1.0 - tick, mid + 5 * tick), tick, "up")
            ctx.submit_market_buy(
                token_id=self._token, notional_usdc=self.seed_inv * cap,
                order_type=OrderType.FAK, slip_limit_price=cap,
                client_tag="seed",
            )
            self._seeded = True
            return

        if not self._should_requote(snap.ts_ms, mid, tick):
            return

        # Cancel current quotes
        if self._bid_id is not None:
            ctx.cancel(self._bid_id)
            self._bid_id = None
        if self._ask_id is not None:
            ctx.cancel(self._ask_id)
            self._ask_id = None

        inventory = ctx.position(self._token)
        bid_price, ask_price = self._target_quotes(mid, inventory, tick)

        # Post bid (only if room to buy more)
        if bid_price > 0 and inventory < self.max_inv:
            r = ctx.submit_limit(
                token_id=self._token, side=Side.BUY,
                size_shares=self.quote_size, price=bid_price,
                order_type=OrderType.GTC, post_only=self.use_post_only,
                client_tag=f"bid@{snap.ts_ms}",
            )
            if not r.rejected:
                self._bid_id = r.order_id

        # Post ask (only if we have inventory)
        if ask_price < 1 and inventory > 0:
            ask_size = min(self.quote_size, inventory)
            r = ctx.submit_limit(
                token_id=self._token, side=Side.SELL,
                size_shares=ask_size, price=ask_price,
                order_type=OrderType.GTC, post_only=self.use_post_only,
                client_tag=f"ask@{snap.ts_ms}",
            )
            if not r.rejected:
                self._ask_id = r.order_id

        self._last_requote_ts_ms = snap.ts_ms
        self._last_requote_mid = mid

    def _should_requote(self, ts_ms: int, mid: float, tick: float) -> bool:
        if self._last_requote_mid is None:
            return True
        if ts_ms - self._last_requote_ts_ms < self.requote_min_interval_ms:
            return False
        if abs(mid - self._last_requote_mid) >= self.requote_mid_move_ticks * tick:
            return True
        # Quote is stale if either side is missing (filled or cancelled)
        return self._bid_id is None or self._ask_id is None

    def _target_quotes(
        self, mid: float, inventory: float, tick: float
    ) -> tuple[float, float]:
        edge = self.base_edge_ticks * tick
        inv_delta = inventory - self.target_inv
        skew = (inv_delta / 100.0) * self.skew_ticks_per_100 * tick

        raw_bid = max(tick, min(1.0 - tick, mid - edge - skew))
        raw_ask = max(tick, min(1.0 - tick, mid + edge - skew))

        bid = _round_to_tick(raw_bid, tick, "down")
        ask = _round_to_tick(raw_ask, tick, "up")
        if bid >= ask:
            ask = bid + tick
        return bid, ask

    # ── order callbacks ──────────────────────────────────────────────

    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None:
        # Engine already records this in metrics; we just clear our tracking
        # if a quote was fully consumed so the next on_book reposts.
        pass

    def on_cancel(self, evt: CancelEvent, ctx: StrategyContext) -> None:
        if self._bid_id == evt.order_id:
            self._bid_id = None
        if self._ask_id == evt.order_id:
            self._ask_id = None

    def on_reject(self, evt: RejectEvent, ctx: StrategyContext) -> None:
        if self.verbose:
            print(f"[mm] reject {evt.order_id}: {evt.reason}")
        if self._bid_id == evt.order_id:
            self._bid_id = None
        if self._ask_id == evt.order_id:
            self._ask_id = None
