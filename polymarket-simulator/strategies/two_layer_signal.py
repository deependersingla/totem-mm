"""Two-layer maker+taker signal strategy.

On each captured 4 / 6 / W signal (assumed to arrive 25s early via the
runner's cricket_lead_ms=25000), we open a $5k position split as:

  bullish side ($2.5k):
    - GTC BUY  $1k worth at best_ask          (crosses → taker portion fills)
    - GTC BUY  $1.5k worth at best_bid        (post_only → rests as maker)

  bearish side ($2.5k):
    - GTC SELL $1k worth at best_bid          (crosses → taker portion fills)
    - GTC SELL $1.5k worth at best_ask        (post_only → rests as maker)

For 4/6 signals: bullish=batting team, bearish=bowling team.
For W signals:   bullish=bowling team, bearish=batting team.

After `unwind_after_ms` (default 10s), we cancel any unfilled entry orders
and post passive unwind orders at the OPPOSITE direction (also GTC):

  whatever we BOUGHT  → SELL at best_ask (passive maker)
  whatever we SOLD    → BUY  at best_bid (passive maker)

If the unwind GTCs don't fill before end of replay, the position carries
to the end and PnL is marked at final mid.

Constraints:
  - skip if either token's mid is outside [min_price, max_price]
  - skip if total deployed budget would exceed budget_usdc
  - signal must be in {4, 6, W}

Inventory: we seed both tokens at start with `seed_shares` each, so the
SELL legs work as literal sells (Polymarket binary CTs cannot be shorted
without owning shares). Unwind brings position back toward seed.
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
    signal_kind: str                          # "4" | "6" | "W"
    bullish_token: str
    bearish_token: str
    deployed_usdc: float                      # for budget bookkeeping
    entry_order_ids: list[str] = field(default_factory=list)
    unwind_order_ids: list[str] = field(default_factory=list)
    bullish_filled_shares: float = 0.0        # net long bullish accumulated
    bearish_filled_shares: float = 0.0        # net short bearish accumulated (positive)
    unwind_started: bool = False


class TwoLayerSignalStrategy(Strategy):
    def __init__(
        self,
        *,
        budget_usdc: float = 20_000.0,
        per_signal_usdc: float = 5_000.0,
        taker_notional_per_side: float = 1_000.0,
        maker_notional_per_side: float = 1_500.0,
        min_price: float = 0.10,
        max_price: float = 0.90,
        unwind_after_ms: int = 10_000,
        signals_to_trade: tuple[str, ...] = ("4", "6", "W"),
        batting_token_index: int = 0,
        seed_shares: float = 100_000.0,
        verbose: bool = False,
        max_signals: int | None = None,
    ):
        self.verbose = verbose
        self.max_signals = max_signals
        self.budget_usdc = budget_usdc
        self.per_signal_usdc = per_signal_usdc
        self.taker_notional = taker_notional_per_side
        self.maker_notional = maker_notional_per_side
        self.min_price = min_price
        self.max_price = max_price
        self.unwind_after_ms = unwind_after_ms
        self.signals_to_trade = set(signals_to_trade)
        self.batting_token_index = batting_token_index
        self.seed_shares = seed_shares

        # Runtime state
        self._batting_token: Optional[str] = None
        self._bowling_token: Optional[str] = None
        self._signals: list[_SignalState] = []
        self._oid_to_signal: dict[str, _SignalState] = {}
        self._deployed_usdc: float = 0.0

        # Counters for the end-of-run report
        self._signals_seen: int = 0
        self._signals_skipped_price: int = 0
        self._signals_skipped_budget: int = 0
        self._signals_skipped_book: int = 0
        self._signals_traded: int = 0

    # ── lifecycle ────────────────────────────────────────────────────

    def on_start(self, ctx: StrategyContext) -> None:
        toks = ctx.market().token_ids
        if self.batting_token_index not in (0, 1):
            raise ValueError(f"batting_token_index must be 0 or 1, got {self.batting_token_index}")
        self._batting_token = toks[self.batting_token_index]
        self._bowling_token = toks[1 - self.batting_token_index]

        # Seed both tokens so SELL legs can work as literal sells
        ctx.seed_position(self._batting_token, self.seed_shares)
        ctx.seed_position(self._bowling_token, self.seed_shares)

    def on_end(self, ctx: StrategyContext) -> None:
        n_unwound = sum(1 for s in self._signals if s.unwind_started)
        n_pending = len(self._signals) - n_unwound

        # Strategy net PnL = cash flow + residual position at current mid,
        # netting out the seed inventory.
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

        # snap.cash_usdc already has fees deducted (Portfolio.apply does that
        # at fill time), so the strategy net PnL is simply cash + residual.
        strategy_pnl = snap.cash_usdc + residual_value

        print(
            f"[two_layer] signals seen={self._signals_seen} traded={self._signals_traded} "
            f"skipped(price={self._signals_skipped_price}, "
            f"budget={self._signals_skipped_budget}, book={self._signals_skipped_book})"
        )
        print(
            f"[two_layer] unwound={n_unwound} pending_unwind={n_pending}"
        )
        print(
            f"[two_layer] residual: bat_pos={bat_pos_above_seed:+.2f} "
            f"@ mid={bat_mid}  bow_pos={bow_pos_above_seed:+.2f} @ mid={bow_mid}"
        )
        print(
            f"[two_layer] strategy_pnl_usdc = cash({snap.cash_usdc:+.2f}) + "
            f"residual_at_mid({residual_value:+.2f}) = NET {strategy_pnl:+.2f}  "
            f"(fees of ${snap.fees_paid_usdc:.2f} already deducted from cash)"
        )

        # Per-signal diagnostic
        for i, sig in enumerate(self._signals):
            print(
                f"  signal[{i}] {sig.signal_kind} @ {sig.signal_ts_ms}: "
                f"bull_shares={sig.bullish_filled_shares:+.1f}  "
                f"bear_shares={sig.bearish_filled_shares:+.1f}  "
                f"unwound={sig.unwind_started}"
            )

    # ── tick check (called on every market event) ────────────────────

    def _check_unwinds(self, ctx: StrategyContext) -> None:
        now = ctx.now_ms()
        for sig in self._signals:
            if sig.unwind_started:
                continue
            if now - sig.signal_ts_ms < self.unwind_after_ms:
                continue
            self._start_unwind(sig, ctx)

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None:
        self._check_unwinds(ctx)

    def on_trade(self, evt: TradeEvent, ctx: StrategyContext) -> None:
        self._check_unwinds(ctx)

    # ── signal handler ───────────────────────────────────────────────

    def on_cricket(self, evt: CricketEvent, ctx: StrategyContext) -> None:
        self._signals_seen += 1
        sig_str = evt.signal.value
        if sig_str not in self.signals_to_trade:
            return

        if self.max_signals is not None and self._signals_traded >= self.max_signals:
            return

        # Budget check
        if self._deployed_usdc + self.per_signal_usdc > self.budget_usdc + 1e-6:
            self._signals_skipped_budget += 1
            return

        bat_book = ctx.book(self._batting_token)
        bow_book = ctx.book(self._bowling_token)
        bat_mid = bat_book.mid
        bow_mid = bow_book.mid
        if bat_mid is None or bow_mid is None:
            self._signals_skipped_book += 1
            return
        if bat_book.best_bid is None or bat_book.best_ask is None:
            self._signals_skipped_book += 1
            return
        if bow_book.best_bid is None or bow_book.best_ask is None:
            self._signals_skipped_book += 1
            return

        # Price bounds (use mid on each side)
        if not (self.min_price <= bat_mid <= self.max_price):
            self._signals_skipped_price += 1
            return
        if not (self.min_price <= bow_mid <= self.max_price):
            self._signals_skipped_price += 1
            return

        # Direction
        if evt.signal == CricketSignal.WICKET:
            bullish_token = self._bowling_token
            bearish_token = self._batting_token
        else:  # 4 or 6
            bullish_token = self._batting_token
            bearish_token = self._bowling_token

        sig = _SignalState(
            signal_ts_ms=evt.ts_ms,
            signal_kind=sig_str,
            bullish_token=bullish_token,
            bearish_token=bearish_token,
            deployed_usdc=self.per_signal_usdc,
        )

        if self.verbose:
            bull_name = "BAT" if bullish_token == self._batting_token else "BOW"
            bear_name = "BOW" if bull_name == "BAT" else "BAT"
            bullish_book = ctx.book(bullish_token)
            bearish_book = ctx.book(bearish_token)
            print()
            print(f"--- SIGNAL #{self._signals_traded+1}: {sig_str} at t={evt.ts_ms} ---")
            print(f"    bullish={bull_name}  bid={bullish_book.best_bid}  ask={bullish_book.best_ask}")
            print(f"    bearish={bear_name}  bid={bearish_book.best_bid}  ask={bearish_book.best_ask}")

        # Submit 4 entry orders. Bullish side = BUYs. Bearish side = SELLs.
        bullish_book = ctx.book(bullish_token)
        bearish_book = ctx.book(bearish_token)
        tag_prefix = f"{sig_str}@{evt.ts_ms}"

        # 1. Bullish taker BUY at best_ask
        self._submit_leg(ctx, sig,
            token=bullish_token, side=Side.BUY,
            price=bullish_book.best_ask, notional=self.taker_notional,
            post_only=False, tag=f"{tag_prefix}/bull_take")

        # 2. Bullish maker BUY at best_bid (post_only)
        self._submit_leg(ctx, sig,
            token=bullish_token, side=Side.BUY,
            price=bullish_book.best_bid, notional=self.maker_notional,
            post_only=True, tag=f"{tag_prefix}/bull_make")

        # 3. Bearish taker SELL at best_bid
        self._submit_leg(ctx, sig,
            token=bearish_token, side=Side.SELL,
            price=bearish_book.best_bid, notional=self.taker_notional,
            post_only=False, tag=f"{tag_prefix}/bear_take")

        # 4. Bearish maker SELL at best_ask (post_only)
        self._submit_leg(ctx, sig,
            token=bearish_token, side=Side.SELL,
            price=bearish_book.best_ask, notional=self.maker_notional,
            post_only=True, tag=f"{tag_prefix}/bear_make")

        self._deployed_usdc += self.per_signal_usdc
        self._signals.append(sig)
        self._signals_traded += 1

    def _submit_leg(
        self, ctx: StrategyContext, sig: _SignalState, *,
        token: str, side: Side, price: float, notional: float,
        post_only: bool, tag: str,
    ) -> None:
        if price <= 0:
            return
        shares = notional / price
        # Round shares to a sane precision (Polymarket allows 2 dp on size)
        shares = round(shares, 2)
        if shares <= 0:
            return
        result = ctx.submit_limit(
            token_id=token, side=side, size_shares=shares, price=price,
            order_type=OrderType.GTC, post_only=post_only,
            client_tag=tag,
        )
        if self.verbose:
            tok_name = "BAT" if token == self._batting_token else "BOW"
            kind = "make" if post_only else "take"
            if result.rejected:
                print(f"    SUBMIT {tok_name} {side.value} {shares:.2f}@{price} {kind} → REJECT {result.reason}")
                return
            sync_fills = sum(f.size_shares for f in result.fills)
            print(f"    SUBMIT {tok_name} {side.value} {shares:.2f}@{price} {kind}"
                  f" → ack {result.order_id}, sync_fills={sync_fills:.2f}")
            for f in result.fills:
                m = "M" if f.is_maker else "T"
                print(f"      → fill {m} {f.size_shares:.2f}@{f.price} fee={f.fee_usdc:.4f}")
        if result.rejected:
            return
        sig.entry_order_ids.append(result.order_id)
        self._oid_to_signal[result.order_id] = sig
        # Also accumulate any synchronous fills (the taker leg fills in submit)
        for f in result.fills:
            self._record_fill(sig, f)

    # ── unwind ───────────────────────────────────────────────────────

    def _start_unwind(self, sig: _SignalState, ctx: StrategyContext) -> None:
        if self.verbose:
            print()
            print(f"--- UNWIND {sig.signal_kind} signal from t={sig.signal_ts_ms} "
                  f"at t={ctx.now_ms()} ---")
            print(f"    bull_filled={sig.bullish_filled_shares:.2f}  "
                  f"bear_filled={sig.bearish_filled_shares:.2f}")

        # Cancel unfilled entry orders
        for oid in sig.entry_order_ids:
            ctx.cancel(oid)

        # Post passive unwind orders for whatever we accumulated
        if sig.bullish_filled_shares > 1e-6:
            book = ctx.book(sig.bullish_token)
            if book.best_ask is not None:
                shares = round(sig.bullish_filled_shares, 2)
                if shares > 0:
                    r = ctx.submit_limit(
                        token_id=sig.bullish_token, side=Side.SELL,
                        size_shares=shares, price=book.best_ask,
                        order_type=OrderType.GTC, post_only=True,
                        client_tag=f"unwind/{sig.signal_kind}@{sig.signal_ts_ms}/bull",
                    )
                    if self.verbose:
                        tok_name = "BAT" if sig.bullish_token == self._batting_token else "BOW"
                        sync = sum(f.size_shares for f in r.fills)
                        print(f"    UNWIND {tok_name} SELL {shares:.2f}@{book.best_ask} make"
                              f" → {'REJECT '+r.reason if r.rejected else f'ack {r.order_id}, sync={sync:.2f}'}")
                        for f in r.fills:
                            m = "M" if f.is_maker else "T"
                            print(f"      → fill {m} {f.size_shares:.2f}@{f.price}")
                    if not r.rejected:
                        sig.unwind_order_ids.append(r.order_id)
                        self._oid_to_signal[r.order_id] = sig
                        for f in r.fills:
                            self._record_fill(sig, f)

        if sig.bearish_filled_shares > 1e-6:
            book = ctx.book(sig.bearish_token)
            if book.best_bid is not None:
                shares = round(sig.bearish_filled_shares, 2)
                if shares > 0:
                    r = ctx.submit_limit(
                        token_id=sig.bearish_token, side=Side.BUY,
                        size_shares=shares, price=book.best_bid,
                        order_type=OrderType.GTC, post_only=True,
                        client_tag=f"unwind/{sig.signal_kind}@{sig.signal_ts_ms}/bear",
                    )
                    if self.verbose:
                        tok_name = "BAT" if sig.bearish_token == self._batting_token else "BOW"
                        sync = sum(f.size_shares for f in r.fills)
                        print(f"    UNWIND {tok_name} BUY  {shares:.2f}@{book.best_bid} make"
                              f" → {'REJECT '+r.reason if r.rejected else f'ack {r.order_id}, sync={sync:.2f}'}")
                        for f in r.fills:
                            m = "M" if f.is_maker else "T"
                            print(f"      → fill {m} {f.size_shares:.2f}@{f.price}")
                    if not r.rejected:
                        sig.unwind_order_ids.append(r.order_id)
                        self._oid_to_signal[r.order_id] = sig
                        for f in r.fills:
                            self._record_fill(sig, f)

        sig.unwind_started = True
        self._deployed_usdc = max(0.0, self._deployed_usdc - sig.deployed_usdc)
        # Release budget when unwind starts. After 10s the entry phase is
        # over; remaining position is being passively walked back. New
        # signals can claim the slot.
        self._deployed_usdc = max(0.0, self._deployed_usdc - sig.deployed_usdc)

    # ── fill / cancel callbacks ──────────────────────────────────────

    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None:
        sig = self._oid_to_signal.get(evt.fill.order_id)
        # Sync fills from submit() arrive here BEFORE _oid_to_signal is set
        # (mapping happens after submit returns). They're already recorded
        # by _submit_leg / _start_unwind via the result.fills loop, so we
        # silently ignore them here. Async fills (resting orders hit by
        # later real trades) have the mapping set by then and we record them.
        if sig is None:
            return
        self._record_fill(sig, evt.fill)
        if self.verbose:
            f = evt.fill
            tok_name = "BAT" if f.token_id == self._batting_token else "BOW"
            m = "M" if f.is_maker else "T"
            print(f"    [t={f.ts_ms}] async FILL {tok_name} {f.side.value} "
                  f"{f.size_shares:.2f}@{f.price} {m} fee={f.fee_usdc:.4f} (order={f.order_id})")

    def _record_fill(self, sig: _SignalState, fill) -> None:
        # Entry fills accumulate; unwind fills decrement.
        is_unwind = fill.order_id in sig.unwind_order_ids
        if fill.token_id == sig.bullish_token:
            if fill.side == Side.BUY:
                sig.bullish_filled_shares += fill.size_shares
            else:  # SELL = unwind
                sig.bullish_filled_shares -= fill.size_shares
        elif fill.token_id == sig.bearish_token:
            if fill.side == Side.SELL:
                sig.bearish_filled_shares += fill.size_shares
            else:  # BUY = unwind
                sig.bearish_filled_shares -= fill.size_shares
        # is_unwind is informational only; the math works either way

    def on_cancel(self, evt: CancelEvent, ctx: StrategyContext) -> None:
        # Could be a user cancel from _start_unwind or an immediate-cancel
        # on a maker that crossed (shouldn't happen since post_only).
        # Budget tracking: we already counted the full per_signal at signal time;
        # we don't release on cancel because we deployed it for the trade.
        pass

    def on_reject(self, evt: RejectEvent, ctx: StrategyContext) -> None:
        # Most likely a post_only that would have crossed (rare with our logic
        # since we read best_bid/best_ask just before submit) or self-trade.
        pass
