"""Maker-only entry + maker revert (zero-fee strategy).

T=0 (signal):
  - BUY  bullish at best_bid (GTC post_only) — joins bid queue, NEVER crosses
  - SELL bearish at best_ask (GTC post_only) — joins ask queue, NEVER crosses

Both rest as makers and pay $0 fee on fill. Fill happens only if real
counterparty hits our level during the entry window.

T=+entry_active_ms (default 10s):
  - Cancel unfilled portions of both entry orders
  - For whatever DID fill, post revert MAKER GTC at entry ± edge*tick:
    - bullish (we bought): SELL at fill_price + edge_ticks * tick
    - bearish (we sold):   BUY  at fill_price - edge_ticks * tick

Both revert legs are also maker (post_only=True) — $0 fee.

Edge: 1 tick for 4/6, 2 ticks for W (configurable).

If the move starts during the 10s entry window, our maker entry fills as
toxic-flow lifts the resting bid/ask. If the window is quiet, fills come
from random MM flow at unbiased prices. Either way: zero entry fees.

Inventory: same seed-100k trick so SELL legs work as literal sells.
on_end nets out seed value before reporting strategy PnL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from backtest.enums import CricketSignal, OrderType, Side
from backtest.events import (
    BookEvent, CancelEvent, CricketEvent, FillEvent, RejectEvent, TradeEvent,
)
from backtest.strategy import Strategy, StrategyContext


def _round_to_tick(price: float, tick: float, direction: str) -> float:
    n = price / tick
    if direction == "down":
        return math.floor(n + 1e-9) * tick
    return math.ceil(n - 1e-9) * tick


@dataclass
class _SignalState:
    signal_ts_ms: int
    signal_kind: str
    revert_ticks: int
    entry_cancel_at_ms: int

    bull_token: str
    bear_token: str

    # entry tracking
    bull_entry_oid: Optional[str] = None
    bear_entry_oid: Optional[str] = None
    bull_filled_shares: float = 0.0
    bull_filled_notional: float = 0.0
    bear_filled_shares: float = 0.0
    bear_filled_notional: float = 0.0

    # revert tracking
    revert_posted: bool = False
    bull_revert_oid: Optional[str] = None
    bear_revert_oid: Optional[str] = None


class MakerEntryRevertStrategy(Strategy):
    def __init__(
        self,
        *,
        budget_usdc: float = 200_000.0,
        per_signal_usdc: float = 200.0,
        entry_notional_per_side: float = 100.0,
        entry_active_ms: int = 10_000,
        revert_ticks_4_6: int = 1,
        revert_ticks_wicket: int = 2,
        min_price: float = 0.10,
        max_price: float = 0.90,
        signals_to_trade: tuple[str, ...] = ("4", "6", "W"),
        batting_token_index: int = 0,
        seed_shares: float = 100_000.0,
        verbose: bool = False,
        max_signals: Optional[int] = None,
    ):
        self.budget_usdc = budget_usdc
        self.per_signal_usdc = per_signal_usdc
        self.entry_notional = entry_notional_per_side
        self.entry_active_ms = entry_active_ms
        self.revert_ticks_4_6 = revert_ticks_4_6
        self.revert_ticks_wicket = revert_ticks_wicket
        self.min_price = min_price
        self.max_price = max_price
        self.signals_to_trade = set(signals_to_trade)
        self.batting_token_index = batting_token_index
        self.seed_shares = seed_shares
        self.verbose = verbose
        self.max_signals = max_signals

        self._batting_token: Optional[str] = None
        self._bowling_token: Optional[str] = None
        self._signals: list[_SignalState] = []
        self._oid_to_signal: dict[str, _SignalState] = {}
        # which leg the order belongs to: "bull_entry"/"bear_entry"/"bull_revert"/"bear_revert"
        self._oid_role: dict[str, str] = {}
        self._deployed_usdc: float = 0.0

        # Diagnostics
        self._signals_seen = 0
        self._signals_skipped_price = 0
        self._signals_skipped_budget = 0
        self._signals_skipped_book = 0
        self._signals_traded = 0
        self._entry_rejected = 0
        self._entry_zero_fill = 0          # signals where neither entry filled at all
        self._entry_full_fill = 0          # signals where both entries fully filled
        self._revert_rejected = 0
        self._reverts_posted = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    def on_start(self, ctx: StrategyContext) -> None:
        toks = ctx.market().token_ids
        if self.batting_token_index not in (0, 1):
            raise ValueError(
                f"batting_token_index must be 0 or 1, got {self.batting_token_index}"
            )
        self._batting_token = toks[self.batting_token_index]
        self._bowling_token = toks[1 - self.batting_token_index]
        ctx.seed_position(self._batting_token, self.seed_shares)
        ctx.seed_position(self._bowling_token, self.seed_shares)

    def on_end(self, ctx: StrategyContext) -> None:
        snap = ctx.pnl()
        bat_pos_above_seed = ctx.position(self._batting_token) - self.seed_shares
        bow_pos_above_seed = ctx.position(self._bowling_token) - self.seed_shares
        bat_mid = ctx.book(self._batting_token).mid
        bow_mid = ctx.book(self._bowling_token).mid
        residual_value = 0.0
        if bat_mid is not None:
            residual_value += bat_pos_above_seed * bat_mid
        if bow_mid is not None:
            residual_value += bow_pos_above_seed * bow_mid
        strategy_pnl = snap.cash_usdc + residual_value

        bat_settle = 1.0 if (bat_mid is not None and bat_mid > 0.5) else 0.0
        bow_settle = 1.0 if (bow_mid is not None and bow_mid > 0.5) else 0.0
        residual_settled = (
            bat_pos_above_seed * bat_settle + bow_pos_above_seed * bow_settle
        )
        strategy_pnl_settled = snap.cash_usdc + residual_settled

        # Count entry fill outcomes
        bull_filled_count = sum(1 for s in self._signals if s.bull_filled_shares > 0)
        bear_filled_count = sum(1 for s in self._signals if s.bear_filled_shares > 0)

        print(
            f"[maker_entry] signals seen={self._signals_seen} traded={self._signals_traded} "
            f"skipped(price={self._signals_skipped_price}, "
            f"budget={self._signals_skipped_budget}, book={self._signals_skipped_book})"
        )
        print(
            f"[maker_entry] entry: bull_filled_legs={bull_filled_count} "
            f"bear_filled_legs={bear_filled_count} "
            f"both_zero={self._entry_zero_fill} entry_rejected={self._entry_rejected} "
            f"reverts_posted={self._reverts_posted} revert_rejected={self._revert_rejected}"
        )
        print(
            f"[maker_entry] residual: bat_pos={bat_pos_above_seed:+.2f} "
            f"@ mid={bat_mid}  bow_pos={bow_pos_above_seed:+.2f} @ mid={bow_mid}"
        )
        print(
            f"[maker_entry] strategy_pnl_usdc = cash({snap.cash_usdc:+.2f}) + "
            f"residual_at_mid({residual_value:+.2f}) = NET {strategy_pnl:+.2f}  "
            f"(fees of ${snap.fees_paid_usdc:.2f} already deducted from cash)"
        )
        print(
            f"[maker_entry] settlement_pnl  = cash({snap.cash_usdc:+.2f}) + "
            f"residual_settled({residual_settled:+.2f}) = SETTLE {strategy_pnl_settled:+.2f}"
        )

    # ── event handlers ────────────────────────────────────────────────

    def _process_due_reverts(self, ctx: StrategyContext, now_ms: int) -> None:
        tick = ctx.market().tick()
        if tick is None:
            return
        for sig in self._signals:
            if sig.revert_posted:
                continue
            if now_ms < sig.entry_cancel_at_ms:
                continue
            self._cancel_entry_post_revert(sig, ctx, tick)

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None:
        self._process_due_reverts(ctx, ctx.now_ms())

    def on_trade(self, evt: TradeEvent, ctx: StrategyContext) -> None:
        self._process_due_reverts(ctx, ctx.now_ms())

    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None:
        oid = evt.fill.order_id
        sig = self._oid_to_signal.get(oid)
        if sig is None:
            return
        role = self._oid_role.get(oid)
        if role == "bull_entry":
            sig.bull_filled_shares += evt.fill.size_shares
            sig.bull_filled_notional += evt.fill.size_shares * evt.fill.price
        elif role == "bear_entry":
            sig.bear_filled_shares += evt.fill.size_shares
            sig.bear_filled_notional += evt.fill.size_shares * evt.fill.price
        # revert fills don't need extra accounting (portfolio handles cash)

    def on_cancel(self, evt: CancelEvent, ctx: StrategyContext) -> None:
        pass

    def on_reject(self, evt: RejectEvent, ctx: StrategyContext) -> None:
        pass

    # ── signal entry (T=0) ────────────────────────────────────────────

    def on_cricket(self, evt: CricketEvent, ctx: StrategyContext) -> None:
        self._signals_seen += 1
        sig_str = evt.signal.value
        if sig_str not in self.signals_to_trade:
            return
        if self.max_signals is not None and self._signals_traded >= self.max_signals:
            return
        if self._deployed_usdc + self.per_signal_usdc > self.budget_usdc + 1e-6:
            self._signals_skipped_budget += 1
            return

        bat_book = ctx.book(self._batting_token)
        bow_book = ctx.book(self._bowling_token)
        bat_mid, bow_mid = bat_book.mid, bow_book.mid
        if bat_mid is None or bow_mid is None:
            self._signals_skipped_book += 1
            return
        if (bat_book.best_bid is None or bat_book.best_ask is None
                or bow_book.best_bid is None or bow_book.best_ask is None):
            self._signals_skipped_book += 1
            return
        if not (self.min_price <= bat_mid <= self.max_price):
            self._signals_skipped_price += 1
            return
        if not (self.min_price <= bow_mid <= self.max_price):
            self._signals_skipped_price += 1
            return

        if evt.signal == CricketSignal.WICKET:
            bull_token = self._bowling_token
            bear_token = self._batting_token
            revert_ticks = self.revert_ticks_wicket
        else:
            bull_token = self._batting_token
            bear_token = self._bowling_token
            revert_ticks = self.revert_ticks_4_6

        bull_book = ctx.book(bull_token)
        bear_book = ctx.book(bear_token)
        tag = f"{sig_str}@{evt.ts_ms}"

        sig = _SignalState(
            signal_ts_ms=evt.ts_ms,
            signal_kind=sig_str,
            revert_ticks=revert_ticks,
            entry_cancel_at_ms=evt.ts_ms + self.entry_active_ms,
            bull_token=bull_token,
            bear_token=bear_token,
        )

        # ── Maker entries (post_only=True) ──
        # 1. BUY bullish at best_bid (joins bid queue)
        bull_price = bull_book.best_bid
        bull_target = round(self.entry_notional / bull_price, 2)
        if bull_target > 0:
            r = ctx.submit_limit(
                token_id=bull_token, side=Side.BUY,
                size_shares=bull_target, price=bull_price,
                order_type=OrderType.GTC, post_only=True,
                client_tag=f"{tag}/bull_entry",
            )
            if r.rejected:
                self._entry_rejected += 1
            else:
                sig.bull_entry_oid = r.order_id
                self._oid_to_signal[r.order_id] = sig
                self._oid_role[r.order_id] = "bull_entry"
                # post_only never has sync fills; nothing to record here

        # 2. SELL bearish at best_ask (joins ask queue)
        bear_price = bear_book.best_ask
        bear_target = round(self.entry_notional / bear_price, 2)
        if bear_target > 0:
            r = ctx.submit_limit(
                token_id=bear_token, side=Side.SELL,
                size_shares=bear_target, price=bear_price,
                order_type=OrderType.GTC, post_only=True,
                client_tag=f"{tag}/bear_entry",
            )
            if r.rejected:
                self._entry_rejected += 1
            else:
                sig.bear_entry_oid = r.order_id
                self._oid_to_signal[r.order_id] = sig
                self._oid_role[r.order_id] = "bear_entry"

        if self.verbose:
            bn = "BAT" if bull_token == self._batting_token else "BOW"
            be = "BOW" if bn == "BAT" else "BAT"
            print(
                f"--- {sig_str}@{evt.ts_ms}  bull={bn} BUY@{bull_price} ({bull_target:.1f}sh)  "
                f"bear={be} SELL@{bear_price} ({bear_target:.1f}sh)  "
                f"cancel/revert in {self.entry_active_ms}ms"
            )

        self._deployed_usdc += self.per_signal_usdc
        self._signals.append(sig)
        self._signals_traded += 1

    # ── entry-cancel + revert post (T=+entry_active_ms) ─────────────

    def _cancel_entry_post_revert(
        self, sig: _SignalState, ctx: StrategyContext, tick: float,
    ) -> None:
        # Cancel any unfilled entry orders. If filled (fully or partially),
        # the remainder is what we're cancelling; the filled shares stay.
        if sig.bull_entry_oid is not None:
            ctx.cancel(sig.bull_entry_oid)
            self._oid_to_signal.pop(sig.bull_entry_oid, None)
            self._oid_role.pop(sig.bull_entry_oid, None)
            sig.bull_entry_oid = None

        if sig.bear_entry_oid is not None:
            ctx.cancel(sig.bear_entry_oid)
            self._oid_to_signal.pop(sig.bear_entry_oid, None)
            self._oid_role.pop(sig.bear_entry_oid, None)
            sig.bear_entry_oid = None

        # Post reverts for whatever filled
        any_revert = False

        if sig.bull_filled_shares > 1e-6:
            avg_price = sig.bull_filled_notional / sig.bull_filled_shares
            target = _round_to_tick(
                avg_price + sig.revert_ticks * tick, tick, "up"
            )
            shares = round(sig.bull_filled_shares, 2)
            if shares > 0 and 0 < target < 1:
                r = ctx.submit_limit(
                    token_id=sig.bull_token, side=Side.SELL,
                    size_shares=shares, price=target,
                    order_type=OrderType.GTC, post_only=True,
                    client_tag=f"revert/{sig.signal_kind}@{sig.signal_ts_ms}/bull",
                )
                if r.rejected:
                    self._revert_rejected += 1
                else:
                    sig.bull_revert_oid = r.order_id
                    self._oid_to_signal[r.order_id] = sig
                    self._oid_role[r.order_id] = "bull_revert"
                    any_revert = True

        if sig.bear_filled_shares > 1e-6:
            avg_price = sig.bear_filled_notional / sig.bear_filled_shares
            target = _round_to_tick(
                avg_price - sig.revert_ticks * tick, tick, "down"
            )
            shares = round(sig.bear_filled_shares, 2)
            if shares > 0 and 0 < target < 1:
                r = ctx.submit_limit(
                    token_id=sig.bear_token, side=Side.BUY,
                    size_shares=shares, price=target,
                    order_type=OrderType.GTC, post_only=True,
                    client_tag=f"revert/{sig.signal_kind}@{sig.signal_ts_ms}/bear",
                )
                if r.rejected:
                    self._revert_rejected += 1
                else:
                    sig.bear_revert_oid = r.order_id
                    self._oid_to_signal[r.order_id] = sig
                    self._oid_role[r.order_id] = "bear_revert"
                    any_revert = True

        if not any_revert and sig.bull_filled_shares == 0 and sig.bear_filled_shares == 0:
            self._entry_zero_fill += 1
        if any_revert:
            self._reverts_posted += 1

        sig.revert_posted = True
        # Release budget
        self._deployed_usdc = max(0.0, self._deployed_usdc - self.per_signal_usdc)
