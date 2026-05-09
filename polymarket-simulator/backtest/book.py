"""L2 order book + a `BookSnapshot` immutable record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from .config import PRICE_EPS
from .enums import Side


@dataclass(frozen=True)
class PriceLevel:
    price: float
    size: float


@dataclass(frozen=True)
class BookSnapshot:
    token_id: str
    ts_ms: int
    bids: tuple[PriceLevel, ...]   # desc by price
    asks: tuple[PriceLevel, ...]   # asc by price

    def all_prices(self) -> Iterator[float]:
        for lv in self.bids:
            yield lv.price
        for lv in self.asks:
            yield lv.price

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2.0
        return self.best_bid if self.bids else self.best_ask


class Book:
    """Mutable view of a token's most recent observed book state."""

    __slots__ = ("token_id", "_snapshot")

    def __init__(self, token_id: str):
        self.token_id = token_id
        self._snapshot: Optional[BookSnapshot] = None

    def apply(self, snapshot: BookSnapshot) -> None:
        if snapshot.token_id != self.token_id:
            raise ValueError(
                f"book {self.token_id} got snapshot for {snapshot.token_id}"
            )
        self._snapshot = snapshot

    @property
    def snapshot(self) -> Optional[BookSnapshot]:
        return self._snapshot

    @property
    def ts_ms(self) -> int:
        return self._snapshot.ts_ms if self._snapshot else 0

    @property
    def best_bid(self) -> Optional[float]:
        return self._snapshot.best_bid if self._snapshot else None

    @property
    def best_ask(self) -> Optional[float]:
        return self._snapshot.best_ask if self._snapshot else None

    @property
    def mid(self) -> Optional[float]:
        return self._snapshot.mid if self._snapshot else None

    @property
    def bids(self) -> tuple[PriceLevel, ...]:
        return self._snapshot.bids if self._snapshot else ()

    @property
    def asks(self) -> tuple[PriceLevel, ...]:
        return self._snapshot.asks if self._snapshot else ()

    def size_at(self, price: float, side: Side) -> float:
        """Visible size at exactly this price level on this side."""
        levels = self.bids if side == Side.BUY else self.asks
        for lv in levels:
            if abs(lv.price - price) <= PRICE_EPS:
                return lv.size
        return 0.0

    def crossable_for(self, side: Side, limit: Optional[float]) -> tuple[PriceLevel, ...]:
        """Levels an aggressive order of `side` can sweep, stopping at `limit`.

        BUY sweeps asks (lowest first); SELL sweeps bids (highest first).
        `limit=None` means no price cap.
        """
        if side == Side.BUY:
            levels = self.asks
            if limit is None:
                return levels
            return tuple(lv for lv in levels if lv.price <= limit + PRICE_EPS)
        else:
            levels = self.bids
            if limit is None:
                return levels
            return tuple(lv for lv in levels if lv.price >= limit - PRICE_EPS)
