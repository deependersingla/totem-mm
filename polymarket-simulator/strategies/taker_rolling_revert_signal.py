"""Taker entry + rolling maker revert (no fees on revert).

T=0 (signal):
  - BUY  bullish at best_ask  (FAK, single shot, takes top-of-book level)
  - SELL bearish at best_bid  (FAK, single shot)
  Pay taker fee here. NEVER re-take afterwards.

T=+3s (initial revert post): GTC post_only, so revert is ALWAYS maker
(zero fee). If post_only would cross (price ran past target), it rejects
and we wait for the next roll cycle.
  - SELL bullish at entry_bull + N * tick
  - BUY  bearish at entry_bear − N * tick

Every roll_interval_ms after that, for each leg that is not yet flat:
  - Cancel the resting revert
  - Re-post (still post_only=True, still same side) at the CURRENT best
    quote on our side: SELL → best_ask, BUY → best_bid. This walks the
    revert toward the live market until it fills as maker.

We never resubmit a taker. If the book moves so far that even maker at
best_ask/bid would let the position carry, the residual rides to end of
replay (same tail-risk as before, but no fees on the revert side).

N = 2 for 4/6 signals, 3 for wickets (configurable).

Inventory: same seed-100k trick so SELL legs work as literal sells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
class _LegState:
    """One revert side (bull-SELL or bear-BUY) for one signal."""
    label: str                    # "bull" | "bear"
    token_id: str
    side: Side                    # SELL for bull, BUY for bear
    entry_price: float            # avg fill price of the entry taker
    target_shares: float          # total shares to revert
    filled_shares: float = 0.0    # how much has filled (sync + async)
    current_oid: Optional[str] = None
    next_action_ms: int = 0       # when to next post or roll
    roll_count: int = 0           # 0 = initial post pending
    is_done: bool = False         # filled_shares ≥ target_shares


@dataclass
class _SignalState:
    signal_ts_ms: int
    signal_kind: str
    revert_ticks: int
    bull_leg: Optional[_LegState] = None
    bear_leg: Optional[_LegState] = None


class TakerRollingRevertStrategy(Strategy):
    def __init__(
        self,
        *,
        budget_usdc: float = 200_000.0,
        per_signal_usdc: float = 5_000.0,
        taker_notional_per_side: float = 2_500.0,
        revert_ticks_4_6: int = 2,
        revert_ticks_wicket: int = 3,
        revert_after_ms: int = 3_000,
        roll_interval_ms: int = 3_000,
        max_rolls: Optional[int] = None,    # None = roll until end of replay
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
        self.taker_notional = taker_notional_per_side
        self.revert_ticks_4_6 = revert_ticks_4_6
        self.revert_ticks_wicket = revert_ticks_wicket
        self.revert_after_ms = revert_after_ms
        self.roll_interval_ms = roll_interval_ms
        self.max_rolls = max_rolls
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
        self._oid_to_leg: dict[str, _LegState] = {}
        self._deployed_usdc: float = 0.0

        # Diagnostics
        self._signals_seen = 0
        self._signals_skipped_price = 0
        self._signals_skipped_budget = 0
        self._signals_skipped_book = 0
        self._signals_traded = 0
        self._takers_zero_fill = 0
        self._initial_post_rejected = 0
        self._roll_post_rejected = 0
        self._roll_count_total = 0

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

        legs_done = 0
        legs_pending = 0
        for sig in self._signals:
            for leg in (sig.bull_leg, sig.bear_leg):
                if leg is None:
                    continue
                if leg.is_done:
                    legs_done += 1
                else:
                    legs_pending += 1

        print(
            f"[rolling] signals seen={self._signals_seen} traded={self._signals_traded} "
            f"skipped(price={self._signals_skipped_price}, "
            f"budget={self._signals_skipped_budget}, book={self._signals_skipped_book}) "
            f"taker_zero_fill={self._takers_zero_fill}"
        )
        print(
            f"[rolling] revert legs: done={legs_done} pending_at_end={legs_pending} "
            f"total_rolls={self._roll_count_total} "
            f"initial_rejected={self._initial_post_rejected} "
            f"roll_rejected={self._roll_post_rejected}"
        )
        print(
            f"[rolling] residual: bat_pos={bat_pos_above_seed:+.2f} "
            f"@ mid={bat_mid}  bow_pos={bow_pos_above_seed:+.2f} @ mid={bow_mid}"
        )
        print(
            f"[rolling] strategy_pnl_usdc = cash({snap.cash_usdc:+.2f}) + "
            f"residual_at_mid({residual_value:+.2f}) = NET {strategy_pnl:+.2f}  "
            f"(fees of ${snap.fees_paid_usdc:.2f} already deducted from cash)"
        )

    # ── event handlers ────────────────────────────────────────────────

    def _drive_legs(self, ctx: StrategyContext) -> None:
        now = ctx.now_ms()
        tick = ctx.market().tick()
        if tick is None:
            return
        for sig in self._signals:
            for leg in (sig.bull_leg, sig.bear_leg):
                if leg is None or leg.is_done:
                    continue
                if now < leg.next_action_ms:
                    continue
                if self.max_rolls is not None and leg.roll_count > self.max_rolls:
                    continue
                self._post_or_roll(leg, sig, ctx, tick, now)

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None:
        self._drive_legs(ctx)

    def on_trade(self, evt: TradeEvent, ctx: StrategyContext) -> None:
        self._drive_legs(ctx)

    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None:
        leg = self._oid_to_leg.get(evt.fill.order_id)
        if leg is None:
            return
        leg.filled_shares += evt.fill.size_shares
        if leg.filled_shares >= leg.target_shares - 1e-6 and not leg.is_done:
            leg.is_done = True
            leg.current_oid = None
            self._deployed_usdc = max(0.0, self._deployed_usdc - self.taker_notional)

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
            bullish_token = self._bowling_token
            bearish_token = self._batting_token
            revert_ticks = self.revert_ticks_wicket
        else:
            bullish_token = self._batting_token
            bearish_token = self._bowling_token
            revert_ticks = self.revert_ticks_4_6

        bull_book = ctx.book(bullish_token)
        bear_book = ctx.book(bearish_token)
        tag = f"{sig_str}@{evt.ts_ms}"

        sig = _SignalState(
            signal_ts_ms=evt.ts_ms,
            signal_kind=sig_str,
            revert_ticks=revert_ticks,
        )

        # ── 2 PURE TAKER legs (single-shot, no re-take) ──
        bull_filled, bull_avg = self._submit_taker(
            ctx, bullish_token, Side.BUY, bull_book.best_ask,
            self.taker_notional, f"{tag}/bull_take",
        )
        bear_filled, bear_avg = self._submit_taker(
            ctx, bearish_token, Side.SELL, bear_book.best_bid,
            self.taker_notional, f"{tag}/bear_take",
        )

        if bull_filled == 0 and bear_filled == 0:
            self._takers_zero_fill += 1

        revert_at = evt.ts_ms + self.revert_after_ms
        deployed = 0.0
        if bull_filled > 0 and bull_avg is not None:
            sig.bull_leg = _LegState(
                label="bull", token_id=bullish_token, side=Side.SELL,
                entry_price=bull_avg, target_shares=bull_filled,
                next_action_ms=revert_at,
            )
            deployed += self.taker_notional
        if bear_filled > 0 and bear_avg is not None:
            sig.bear_leg = _LegState(
                label="bear", token_id=bearish_token, side=Side.BUY,
                entry_price=bear_avg, target_shares=bear_filled,
                next_action_ms=revert_at,
            )
            deployed += self.taker_notional

        if self.verbose:
            bn = "BAT" if bullish_token == self._batting_token else "BOW"
            be = "BOW" if bn == "BAT" else "BAT"
            print(
                f"--- {sig_str}@{evt.ts_ms}  bull={bn} take@{bull_avg} "
                f"({bull_filled:.1f}sh)  bear={be} take@{bear_avg} "
                f"({bear_filled:.1f}sh)  revert@+{revert_ticks}t in {self.revert_after_ms}ms"
            )

        self._deployed_usdc += deployed
        self._signals.append(sig)
        self._signals_traded += 1

    def _submit_taker(
        self, ctx: StrategyContext, token_id: str, side: Side,
        price: float, notional: float, tag: str,
    ) -> tuple[float, Optional[float]]:
        if side == Side.BUY:
            r = ctx.submit_market_buy(
                token_id=token_id, notional_usdc=notional,
                order_type=OrderType.FAK, slip_limit_price=price,
                client_tag=tag,
            )
        else:
            target_shares = round(notional / price, 2)
            if target_shares <= 0:
                return 0.0, None
            r = ctx.submit_market_sell(
                token_id=token_id, size_shares=target_shares,
                order_type=OrderType.FAK, slip_limit_price=price,
                client_tag=tag,
            )
        if r.rejected or not r.fills:
            return 0.0, None
        filled = sum(f.size_shares for f in r.fills)
        notional_filled = sum(f.size_shares * f.price for f in r.fills)
        return filled, (notional_filled / filled if filled > 0 else None)

    # ── revert post / roll ────────────────────────────────────────────

    def _post_or_roll(
        self, leg: _LegState, sig: _SignalState,
        ctx: StrategyContext, tick: float, now_ms: int,
    ) -> None:
        remaining = round(leg.target_shares - leg.filled_shares, 2)
        if remaining <= 0:
            leg.is_done = True
            return

        # Cancel any prior resting revert before re-posting
        if leg.current_oid is not None:
            ctx.cancel(leg.current_oid)
            self._oid_to_leg.pop(leg.current_oid, None)
            leg.current_oid = None

        book = ctx.book(leg.token_id)
        is_initial = (leg.roll_count == 0)

        if leg.side == Side.SELL:
            if is_initial:
                target_price = _round_to_tick(
                    leg.entry_price + sig.revert_ticks * tick, tick, "up"
                )
            else:
                if book.best_ask is None:
                    leg.next_action_ms = now_ms + self.roll_interval_ms
                    return
                target_price = book.best_ask
        else:  # Side.BUY
            if is_initial:
                target_price = _round_to_tick(
                    leg.entry_price - sig.revert_ticks * tick, tick, "down"
                )
            else:
                if book.best_bid is None:
                    leg.next_action_ms = now_ms + self.roll_interval_ms
                    return
                target_price = book.best_bid

        if target_price <= 0 or target_price >= 1:
            leg.next_action_ms = now_ms + self.roll_interval_ms
            return

        r = ctx.submit_limit(
            token_id=leg.token_id, side=leg.side,
            size_shares=remaining, price=target_price,
            order_type=OrderType.GTC, post_only=True,
            client_tag=(
                f"revert/{sig.signal_kind}@{sig.signal_ts_ms}/{leg.label}"
                f"#{leg.roll_count}"
            ),
        )
        leg.next_action_ms = now_ms + self.roll_interval_ms
        leg.roll_count += 1
        if leg.roll_count > 1:
            self._roll_count_total += 1

        if r.rejected:
            if is_initial:
                self._initial_post_rejected += 1
            else:
                self._roll_post_rejected += 1
            return

        # Synchronous fills (rare for post_only=True since it can't cross)
        for f in r.fills:
            leg.filled_shares += f.size_shares

        if leg.filled_shares >= leg.target_shares - 1e-6:
            if not leg.is_done:
                leg.is_done = True
                self._deployed_usdc = max(
                    0.0, self._deployed_usdc - self.taker_notional
                )
            return

        leg.current_oid = r.order_id
        self._oid_to_leg[r.order_id] = leg
