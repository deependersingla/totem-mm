"""Taker entry + maker revert + taker FALLBACK unwind at 12s.

Timeline per signal:
  T = 0     — 2 FAK taker entries (BUY bullish best_ask, SELL bearish best_bid)
  T = +3s   — 2 maker GTC reverts (post_only) at entry ± edge*tick (zero fee)
  T = +12s  — if revert hasn't fully filled, cancel and market-order the
              remainder to flatten. Eats spread + 3% fee on the fallback,
              but caps residual at "1-2 ticks past entry" instead of "fully
              marked at terminal $0/$1".

Edge: 1 tick for 4/6, 2 ticks for W (configurable).

Three outcomes per leg, in order of profitability:
  1. Revert maker fills inside 12s window → +edge ticks profit, $0 fee on exit.
  2. Revert maker doesn't fill → fallback market order flushes at current
     bid/ask, paying spread + fee. Loss is bounded.
  3. Market order can't fill (no liquidity) → residual still rides; rare.
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
    signal_kind: str
    revert_ticks: int
    revert_at_ms: int
    unwind_at_ms: int

    bull_token: str
    bear_token: str

    # entry fills (set synchronously at T=0)
    bull_filled_shares: float = 0.0
    bull_avg_price: Optional[float] = None
    bear_filled_shares: float = 0.0
    bear_avg_price: Optional[float] = None

    # revert state
    revert_posted: bool = False
    bull_revert_oid: Optional[str] = None
    bear_revert_oid: Optional[str] = None
    bull_revert_filled: float = 0.0
    bear_revert_filled: float = 0.0

    # unwind state
    unwind_started: bool = False


class TakerRevertWithUnwindStrategy(Strategy):
    def __init__(
        self,
        *,
        budget_usdc: float = 200_000.0,
        per_signal_usdc: float = 200.0,
        taker_notional_per_side: float = 100.0,
        revert_after_ms: int = 3_000,
        unwind_after_ms: int = 600_000,    # 10 minutes — let GTC fill via slow-tail moves
        revert_ticks_4_6: int = 2,
        revert_ticks_wicket: int = 3,
        min_price: float = 0.10,
        max_price: float = 0.90,
        signals_to_trade: tuple[str, ...] = ("6", "W"),
        batting_token_index: int = 0,
        seed_shares: float = 100_000.0,
        verbose: bool = False,
        max_signals: Optional[int] = None,
    ):
        self.budget_usdc = budget_usdc
        self.per_signal_usdc = per_signal_usdc
        self.taker_notional = taker_notional_per_side
        self.revert_after_ms = revert_after_ms
        self.unwind_after_ms = unwind_after_ms
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
        self._oid_role: dict[str, str] = {}      # bull_revert / bear_revert
        self._deployed_usdc: float = 0.0

        # Diagnostics
        self._signals_seen = 0
        self._signals_skipped_price = 0
        self._signals_skipped_budget = 0
        self._signals_skipped_book = 0
        self._signals_traded = 0
        self._taker_zero_fill = 0
        self._reverts_posted = 0
        self._reverts_rejected = 0
        self._unwinds_triggered_legs = 0
        self._unwinds_filled_legs = 0
        self._unwinds_no_liquidity = 0

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
        # End-of-match force-flush. Cancel all open reverts and market-flatten
        # any residual position. Eats spread + fee but caps catastrophic loss.
        # NOTE: there are no more book/trade events after this so any new
        # taker submission matches against the LAST observed book — which is
        # near-settlement (mid 0.001/0.999), so flush yields prices very
        # close to actual settle value. For losing-side positions this still
        # locks in the loss; the goal is just to convert stale residual into
        # final cash so we have a clean accounting.
        for sig in self._signals:
            if not sig.unwind_started:
                self._fallback_unwind(sig, ctx)

        snap = ctx.pnl()
        bat_pos = ctx.position(self._batting_token) - self.seed_shares
        bow_pos = ctx.position(self._bowling_token) - self.seed_shares
        bat_mid = ctx.book(self._batting_token).mid
        bow_mid = ctx.book(self._bowling_token).mid
        residual = 0.0
        if bat_mid is not None: residual += bat_pos * bat_mid
        if bow_mid is not None: residual += bow_pos * bow_mid
        net = snap.cash_usdc + residual

        bat_settle = 1.0 if (bat_mid is not None and bat_mid > 0.5) else 0.0
        bow_settle = 1.0 if (bow_mid is not None and bow_mid > 0.5) else 0.0
        residual_set = bat_pos * bat_settle + bow_pos * bow_settle
        net_set = snap.cash_usdc + residual_set

        print(
            f"[unwind] signals seen={self._signals_seen} traded={self._signals_traded} "
            f"skipped(price={self._signals_skipped_price}, "
            f"budget={self._signals_skipped_budget}, book={self._signals_skipped_book}) "
            f"taker_zero_fill={self._taker_zero_fill}"
        )
        print(
            f"[unwind] reverts_posted={self._reverts_posted} "
            f"reverts_rejected={self._reverts_rejected}  "
            f"unwind_legs_triggered={self._unwinds_triggered_legs} "
            f"unwind_legs_filled={self._unwinds_filled_legs} "
            f"unwind_legs_no_liq={self._unwinds_no_liquidity}"
        )
        print(
            f"[unwind] residual: bat_pos={bat_pos:+.2f} @ mid={bat_mid}  "
            f"bow_pos={bow_pos:+.2f} @ mid={bow_mid}"
        )
        print(
            f"[unwind] strategy_pnl_usdc = cash({snap.cash_usdc:+.2f}) + "
            f"residual_at_mid({residual:+.2f}) = NET {net:+.2f}  "
            f"(fees of ${snap.fees_paid_usdc:.2f} already deducted from cash)"
        )
        print(
            f"[unwind] settlement_pnl  = cash({snap.cash_usdc:+.2f}) + "
            f"residual_settled({residual_set:+.2f}) = SETTLE {net_set:+.2f}"
        )

    # ── event handlers ────────────────────────────────────────────────

    def _drive(self, ctx: StrategyContext) -> None:
        now = ctx.now_ms()
        tick = ctx.market().tick()
        if tick is None:
            return
        for sig in self._signals:
            # Step 1: post revert at T+revert_after_ms
            if not sig.revert_posted and now >= sig.revert_at_ms:
                self._post_reverts(sig, ctx, tick)
            # Step 2: at T+unwind_after_ms, fallback to market unwind for any
            # leg whose revert hasn't fully filled
            if (sig.revert_posted and not sig.unwind_started
                    and now >= sig.unwind_at_ms):
                self._fallback_unwind(sig, ctx)

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None:
        self._drive(ctx)

    def on_trade(self, evt: TradeEvent, ctx: StrategyContext) -> None:
        self._drive(ctx)

    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None:
        oid = evt.fill.order_id
        sig = self._oid_to_signal.get(oid)
        if sig is None:
            return
        role = self._oid_role.get(oid)
        if role == "bull_revert":
            sig.bull_revert_filled += evt.fill.size_shares
        elif role == "bear_revert":
            sig.bear_revert_filled += evt.fill.size_shares

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
        if bat_book.mid is None or bow_book.mid is None:
            self._signals_skipped_book += 1
            return
        if (bat_book.best_bid is None or bat_book.best_ask is None
                or bow_book.best_bid is None or bow_book.best_ask is None):
            self._signals_skipped_book += 1
            return
        if not (self.min_price <= bat_book.mid <= self.max_price):
            self._signals_skipped_price += 1
            return
        if not (self.min_price <= bow_book.mid <= self.max_price):
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
            revert_at_ms=evt.ts_ms + self.revert_after_ms,
            unwind_at_ms=evt.ts_ms + self.unwind_after_ms,
            bull_token=bull_token,
            bear_token=bear_token,
        )

        # ── 1. Bullish TAKER entry (BUY at best_ask, FAK) ──
        r = ctx.submit_market_buy(
            token_id=bull_token,
            notional_usdc=self.taker_notional,
            order_type=OrderType.FAK,
            slip_limit_price=bull_book.best_ask,
            client_tag=f"{tag}/bull_take",
        )
        if not r.rejected and r.fills:
            shares = sum(f.size_shares for f in r.fills)
            notional = sum(f.size_shares * f.price for f in r.fills)
            sig.bull_filled_shares = shares
            sig.bull_avg_price = notional / shares if shares > 0 else None

        # ── 2. Bearish TAKER entry (SELL at best_bid, FAK) ──
        bear_taker_shares = round(self.taker_notional / bear_book.best_bid, 2)
        if bear_taker_shares > 0:
            r = ctx.submit_market_sell(
                token_id=bear_token,
                size_shares=bear_taker_shares,
                order_type=OrderType.FAK,
                slip_limit_price=bear_book.best_bid,
                client_tag=f"{tag}/bear_take",
            )
            if not r.rejected and r.fills:
                shares = sum(f.size_shares for f in r.fills)
                notional = sum(f.size_shares * f.price for f in r.fills)
                sig.bear_filled_shares = shares
                sig.bear_avg_price = notional / shares if shares > 0 else None

        if sig.bull_filled_shares == 0 and sig.bear_filled_shares == 0:
            self._taker_zero_fill += 1

        if self.verbose:
            bn = "BAT" if bull_token == self._batting_token else "BOW"
            be = "BOW" if bn == "BAT" else "BAT"
            print(
                f"--- {sig_str}@{evt.ts_ms}  bull={bn} take@{sig.bull_avg_price} "
                f"({sig.bull_filled_shares:.1f}sh)  bear={be} take@{sig.bear_avg_price} "
                f"({sig.bear_filled_shares:.1f}sh)  revert@+{revert_ticks}t in "
                f"{self.revert_after_ms}ms, unwind in {self.unwind_after_ms}ms"
            )

        self._deployed_usdc += self.per_signal_usdc
        self._signals.append(sig)
        self._signals_traded += 1

    # ── revert post (T = T0 + revert_after_ms) ────────────────────────

    def _post_reverts(
        self, sig: _SignalState, ctx: StrategyContext, tick: float,
    ) -> None:
        any_posted = False

        # Bull revert: SELL at avg + N*tick (post_only, $0 fee)
        if sig.bull_filled_shares > 1e-6 and sig.bull_avg_price is not None:
            target = _round_to_tick(
                sig.bull_avg_price + sig.revert_ticks * tick, tick, "up"
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
                    self._reverts_rejected += 1
                else:
                    sig.bull_revert_oid = r.order_id
                    self._oid_to_signal[r.order_id] = sig
                    self._oid_role[r.order_id] = "bull_revert"
                    any_posted = True

        # Bear revert: BUY at avg - N*tick (post_only, $0 fee)
        if sig.bear_filled_shares > 1e-6 and sig.bear_avg_price is not None:
            target = _round_to_tick(
                sig.bear_avg_price - sig.revert_ticks * tick, tick, "down"
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
                    self._reverts_rejected += 1
                else:
                    sig.bear_revert_oid = r.order_id
                    self._oid_to_signal[r.order_id] = sig
                    self._oid_role[r.order_id] = "bear_revert"
                    any_posted = True

        if any_posted:
            self._reverts_posted += 1
        sig.revert_posted = True

    # ── fallback market unwind (T = T0 + unwind_after_ms) ────────────

    def _fallback_unwind(
        self, sig: _SignalState, ctx: StrategyContext,
    ) -> None:
        sig.unwind_started = True

        # Bull leg: cancel revert, market-sell the remaining (long position)
        bull_remaining = round(sig.bull_filled_shares - sig.bull_revert_filled, 2)
        if bull_remaining > 1e-6:
            self._unwinds_triggered_legs += 1
            if sig.bull_revert_oid is not None:
                ctx.cancel(sig.bull_revert_oid)
                self._oid_to_signal.pop(sig.bull_revert_oid, None)
                self._oid_role.pop(sig.bull_revert_oid, None)
                sig.bull_revert_oid = None
            r = ctx.submit_market_sell(
                token_id=sig.bull_token,
                size_shares=bull_remaining,
                order_type=OrderType.FAK,
                slip_limit_price=None,    # no slip cap — flush at any price
                client_tag=f"unwind/{sig.signal_kind}@{sig.signal_ts_ms}/bull",
            )
            if r.rejected or not r.fills:
                self._unwinds_no_liquidity += 1
            else:
                self._unwinds_filled_legs += 1

        # Bear leg: cancel revert, market-buy the remaining (short position)
        bear_remaining = round(sig.bear_filled_shares - sig.bear_revert_filled, 2)
        if bear_remaining > 1e-6:
            self._unwinds_triggered_legs += 1
            if sig.bear_revert_oid is not None:
                ctx.cancel(sig.bear_revert_oid)
                self._oid_to_signal.pop(sig.bear_revert_oid, None)
                self._oid_role.pop(sig.bear_revert_oid, None)
                sig.bear_revert_oid = None
            # Buy back what we sold short. notional = remaining * best_ask
            # so the matcher fills up to bear_remaining shares — never more.
            # If the book is thin and we slip to higher prices, we under-fill
            # by a few shares (acceptable). Cap slip at 10 ticks to bound risk.
            book = ctx.book(sig.bear_token)
            tick = ctx.market().tick() or 0.01
            best_ask = book.best_ask
            if best_ask is None:
                self._unwinds_no_liquidity += 1
            else:
                slip_cap = min(0.99, best_ask + 10 * tick)
                notional = bear_remaining * best_ask    # caps shares to ≤ bear_remaining
                r = ctx.submit_market_buy(
                    token_id=sig.bear_token,
                    notional_usdc=notional,
                    order_type=OrderType.FAK,
                    slip_limit_price=slip_cap,
                    client_tag=f"unwind/{sig.signal_kind}@{sig.signal_ts_ms}/bear",
                )
                if r.rejected or not r.fills:
                    self._unwinds_no_liquidity += 1
                else:
                    self._unwinds_filled_legs += 1

        # Release budget
        self._deployed_usdc = max(0.0, self._deployed_usdc - self.per_signal_usdc)
