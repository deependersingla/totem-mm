"""Track simulated positions, P&L, and exposure."""

from __future__ import annotations

from .models import OrderBookSnapshot, Side, SimFill


class PositionTracker:
    def __init__(self, initial_cash: float = 10_000.0):
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.token_balances: dict[str, float] = {}    # token_id -> net tokens
        self.cost_basis: dict[str, float] = {}         # token_id -> total cost
        self.realized_pnl: float = 0.0
        self.fills: list[SimFill] = []
        self.trade_count: int = 0

    def on_fill(self, fill: SimFill):
        self.fills.append(fill)
        self.trade_count += 1
        tid = fill.token_id
        notional = fill.price * fill.size

        if fill.side == Side.BUY:
            self.cash -= notional
            self.token_balances[tid] = self.token_balances.get(tid, 0) + fill.size
            self.cost_basis[tid] = self.cost_basis.get(tid, 0) + notional
        else:
            self.cash += notional
            prev_balance = self.token_balances.get(tid, 0)
            if prev_balance > 0:
                # Realize P&L: proceeds - proportional cost basis
                avg_cost = self.cost_basis.get(tid, 0) / prev_balance if prev_balance else 0
                cost_of_sold = avg_cost * fill.size
                self.realized_pnl += notional - cost_of_sold
                self.cost_basis[tid] = max(0, self.cost_basis.get(tid, 0) - cost_of_sold)
            self.token_balances[tid] = self.token_balances.get(tid, 0) - fill.size

    def unrealized_pnl(self, books: dict[str, OrderBookSnapshot]) -> float:
        pnl = 0.0
        for tid, balance in self.token_balances.items():
            if abs(balance) < 0.001:
                continue
            book = books.get(tid)
            if not book or not book.mid_price:
                continue
            market_value = balance * book.mid_price
            cost = self.cost_basis.get(tid, 0)
            if balance > 0:
                pnl += market_value - cost
            else:
                pnl += cost - abs(market_value)
        return pnl

    def total_pnl(self, books: dict[str, OrderBookSnapshot]) -> float:
        return self.realized_pnl + self.unrealized_pnl(books)

    def exposure(self, books: dict[str, OrderBookSnapshot]) -> float:
        total = 0.0
        for tid, balance in self.token_balances.items():
            if abs(balance) < 0.001:
                continue
            book = books.get(tid)
            mid = book.mid_price if book else None
            if mid:
                total += abs(balance * mid)
        return total

    def to_dict(self, books: dict[str, OrderBookSnapshot] | None = None) -> dict:
        books = books or {}
        return {
            "cash": round(self.cash, 2),
            "initial_cash": self.initial_cash,
            "token_balances": {k: round(v, 2) for k, v in self.token_balances.items() if abs(v) > 0.001},
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl(books), 2),
            "total_pnl": round(self.total_pnl(books), 2),
            "exposure": round(self.exposure(books), 2),
            "trade_count": self.trade_count,
        }
