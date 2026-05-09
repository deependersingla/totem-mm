"""Taker entry + delayed maker revert.

T=0 (signal): two PURE TAKER legs at top of book — pay fee, FAK so any
unfilled remainder is cancelled (no resting bids/asks left behind):
  - BUY  bullish at best_ask
  - SELL bearish at best_bid

T=+3s: two REVERT legs at entry_price ± N ticks. Submitted as plain
GTC limits so they naturally settle as maker if market hasn't run past
target, or cross as taker if it already has (locking in ≥ N-tick profit
either way):
  - SELL bullish at entry_bull + N * tick
  - BUY  bearish at entry_bear − N * tick

N = 2 for 4/6 signals, 3 for wickets (configurable).

Inventory: same seed-100k trick as two_layer so SELL legs work as
literal sells. on_end nets the seed out before reporting strategy PnL.
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
class _SignalState:
    signal_ts_ms: int
    signal_kind: str                          # "4" | "6" | "W"
    bullish_token: str
    bearish_token: str
    revert_at_ms: int
    revert_ticks: int
    bull_filled_shares: float = 0.0
    bull_avg_price: Optional[float] = None
    bear_filled_shares: float = 0.0
    bear_avg_price: Optional[float] = None
    revert_submitted: bool = False
    bull_revert_oid: Optional[str] = None
    bear_revert_oid: Optional[str] = None


class TakerRevertSignalStrategy(Strategy):
    def __init__(
        self,
        *,
        budget_usdc: float = 10_000.0,
        per_signal_usdc: float = 200.0,
        taker_notional_per_side: float = 100.0,
        revert_ticks_4_6: int = 2,
        revert_ticks_wicket: int = 3,
        revert_after_ms: int = 3_000,
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
        self._deployed_usdc: float = 0.0

        self._signals_seen: int = 0
        self._signals_skipped_price: int = 0
        self._signals_skipped_budget: int = 0
        self._signals_skipped_book: int = 0
        self._signals_traded: int = 0
        self._takers_zero_fill: int = 0
        self._reverts_rejected: int = 0
        self._reverts_sync_taker: int = 0
        self._reverts_async_maker: int = 0

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

        # Settlement-marked: round each token's residual to $1 (mid > 0.5
        # → winner) or $0 (loser). Approximates Polymarket binary settlement.
        bat_settle = 1.0 if (bat_mid is not None and bat_mid > 0.5) else 0.0
        bow_settle = 1.0 if (bow_mid is not None and bow_mid > 0.5) else 0.0
        residual_settled = (
            bat_pos_above_seed * bat_settle + bow_pos_above_seed * bow_settle
        )
        strategy_pnl_settled = snap.cash_usdc + residual_settled

        n_pending_revert = sum(1 for s in self._signals if not s.revert_submitted)

        print(
            f"[taker_revert] signals seen={self._signals_seen} traded={self._signals_traded} "
            f"skipped(price={self._signals_skipped_price}, "
            f"budget={self._signals_skipped_budget}, book={self._signals_skipped_book}) "
            f"taker_zero_fill={self._takers_zero_fill}"
        )
        print(
            f"[taker_revert] reverts: rejected={self._reverts_rejected} "
            f"sync_taker={self._reverts_sync_taker} async_maker={self._reverts_async_maker} "
            f"pending_at_end={n_pending_revert}"
        )
        print(
            f"[taker_revert] residual: bat_pos={bat_pos_above_seed:+.2f} "
            f"@ mid={bat_mid}  bow_pos={bow_pos_above_seed:+.2f} @ mid={bow_mid}"
        )
        print(
            f"[taker_revert] strategy_pnl_usdc = cash({snap.cash_usdc:+.2f}) + "
            f"residual_at_mid({residual_value:+.2f}) = NET {strategy_pnl:+.2f}  "
            f"(fees of ${snap.fees_paid_usdc:.2f} already deducted from cash)"
        )
        print(
            f"[taker_revert] settlement_pnl  = cash({snap.cash_usdc:+.2f}) + "
            f"residual_settled({residual_settled:+.2f}) = SETTLE {strategy_pnl_settled:+.2f}"
        )

    # ── revert scheduler (called on every market event) ──────────────

    def _check_reverts(self, ctx: StrategyContext) -> None:
        now = ctx.now_ms()
        tick = ctx.market().tick()
        for sig in self._signals:
            if sig.revert_submitted:
                continue
            if now < sig.revert_at_ms:
                continue
            self._submit_revert(sig, ctx, tick)

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None:
        self._check_reverts(ctx)

    def on_trade(self, evt: TradeEvent, ctx: StrategyContext) -> None:
        self._check_reverts(ctx)

    # ── signal handler ───────────────────────────────────────────────

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
        else:  # 4 or 6
            bullish_token = self._batting_token
            bearish_token = self._bowling_token
            revert_ticks = self.revert_ticks_4_6

        sig = _SignalState(
            signal_ts_ms=evt.ts_ms,
            signal_kind=sig_str,
            bullish_token=bullish_token,
            bearish_token=bearish_token,
            revert_at_ms=evt.ts_ms + self.revert_after_ms,
            revert_ticks=revert_ticks,
        )

        bull_book = ctx.book(bullish_token)
        bear_book = ctx.book(bearish_token)
        tag = f"{sig_str}@{evt.ts_ms}"

        # ── 2 PURE TAKER legs (market FAK with slip = top-of-book) ──
        # 1. BUY bullish at best_ask — slip_limit = best_ask so we only
        # take the level at the ask, no deeper sweep, nothing rests.
        bull_price = bull_book.best_ask
        r = ctx.submit_market_buy(
            token_id=bullish_token,
            notional_usdc=self.taker_notional,
            order_type=OrderType.FAK,
            slip_limit_price=bull_price,
            client_tag=f"{tag}/bull_take",
        )
        if not r.rejected and r.fills:
            filled = sum(f.size_shares for f in r.fills)
            notional = sum(f.size_shares * f.price for f in r.fills)
            sig.bull_filled_shares = filled
            sig.bull_avg_price = notional / filled if filled > 0 else None

        # 2. SELL bearish at best_bid
        bear_price = bear_book.best_bid
        bear_target_shares = round(self.taker_notional / bear_price, 2)
        if bear_target_shares > 0:
            r = ctx.submit_market_sell(
                token_id=bearish_token,
                size_shares=bear_target_shares,
                order_type=OrderType.FAK,
                slip_limit_price=bear_price,
                client_tag=f"{tag}/bear_take",
            )
            if not r.rejected and r.fills:
                filled = sum(f.size_shares for f in r.fills)
                notional = sum(f.size_shares * f.price for f in r.fills)
                sig.bear_filled_shares = filled
                sig.bear_avg_price = notional / filled if filled > 0 else None

        if sig.bull_filled_shares == 0 and sig.bear_filled_shares == 0:
            self._takers_zero_fill += 1

        if self.verbose:
            bn = "BAT" if bullish_token == self._batting_token else "BOW"
            be = "BOW" if bn == "BAT" else "BAT"
            print(
                f"--- {sig_str}@{evt.ts_ms}  bull={bn} take@{sig.bull_avg_price} "
                f"({sig.bull_filled_shares:.1f}sh)  bear={be} take@{sig.bear_avg_price} "
                f"({sig.bear_filled_shares:.1f}sh)  revert@+{sig.revert_ticks}t in {self.revert_after_ms}ms"
            )

        self._deployed_usdc += self.per_signal_usdc
        self._signals.append(sig)
        self._signals_traded += 1

    # ── revert submission ────────────────────────────────────────────

    def _submit_revert(self, sig: _SignalState, ctx: StrategyContext, tick: float) -> None:
        # Bull side: SELL at entry + N*tick
        if sig.bull_filled_shares > 1e-6 and sig.bull_avg_price is not None:
            target = _round_to_tick(
                sig.bull_avg_price + sig.revert_ticks * tick, tick, "up"
            )
            shares = round(sig.bull_filled_shares, 2)
            if shares > 0 and target > 0:
                r = ctx.submit_limit(
                    token_id=sig.bullish_token, side=Side.SELL,
                    size_shares=shares, price=target,
                    order_type=OrderType.GTC, post_only=False,
                    client_tag=f"revert/{sig.signal_kind}@{sig.signal_ts_ms}/bull",
                )
                if r.rejected:
                    self._reverts_rejected += 1
                else:
                    sig.bull_revert_oid = r.order_id
                    self._oid_to_signal[r.order_id] = sig
                    if r.fills:
                        self._reverts_sync_taker += 1
                    if self.verbose:
                        sf = sum(f.size_shares for f in r.fills)
                        print(
                            f"    revert bull SELL {shares:.2f}@{target} "
                            f"→ ack {r.order_id}, sync={sf:.2f}"
                        )

        # Bear side: BUY at entry − N*tick
        if sig.bear_filled_shares > 1e-6 and sig.bear_avg_price is not None:
            target = _round_to_tick(
                sig.bear_avg_price - sig.revert_ticks * tick, tick, "down"
            )
            shares = round(sig.bear_filled_shares, 2)
            if shares > 0 and target > 0:
                r = ctx.submit_limit(
                    token_id=sig.bearish_token, side=Side.BUY,
                    size_shares=shares, price=target,
                    order_type=OrderType.GTC, post_only=False,
                    client_tag=f"revert/{sig.signal_kind}@{sig.signal_ts_ms}/bear",
                )
                if r.rejected:
                    self._reverts_rejected += 1
                else:
                    sig.bear_revert_oid = r.order_id
                    self._oid_to_signal[r.order_id] = sig
                    if r.fills:
                        self._reverts_sync_taker += 1
                    if self.verbose:
                        sf = sum(f.size_shares for f in r.fills)
                        print(
                            f"    revert bear BUY  {shares:.2f}@{target} "
                            f"→ ack {r.order_id}, sync={sf:.2f}"
                        )

        sig.revert_submitted = True
        self._deployed_usdc = max(0.0, self._deployed_usdc - self.per_signal_usdc)

    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None:
        sig = self._oid_to_signal.get(evt.fill.order_id)
        if sig is None:
            return
        if evt.fill.is_maker:
            self._reverts_async_maker += 1

    def on_cancel(self, evt: CancelEvent, ctx: StrategyContext) -> None:
        pass

    def on_reject(self, evt: RejectEvent, ctx: StrategyContext) -> None:
        pass
