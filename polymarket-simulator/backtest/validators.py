"""Order validation against Polymarket CLOB rules.

Validation pulls the *current* tick from the market (which is updated by the
engine on every book snapshot), so dynamic tick changes mid-match are
honored automatically.
"""

from __future__ import annotations

from typing import Optional

from .config import (
    FOK_SELL_MAKER_AMOUNT_DECIMALS,
    FOK_SELL_TAKER_AMOUNT_DECIMALS,
    GTD_MIN_LIFETIME_S,
    PRICE_EPS,
)
from .enums import OrderType, Side
from .market import Market
from .tick import is_multiple_of_tick


class OrderRejection(Exception):
    """Raised when an order violates a Polymarket CLOB rule.

    The string form is intended to mirror the live CLOB error so strategy
    code sees the same `reason` it would in production.
    """


def _decimals_in(x: float) -> int:
    s = f"{x:.10f}".rstrip("0")
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])


def _validate_price_on_tick(price: float, market: Market) -> None:
    if not (0 < price < 1):
        raise OrderRejection(f"price {price} must be strictly between 0 and 1")
    tick = market.tick()
    if tick is None:
        # No book observed yet; accept any 4-dp price (most permissive valid tick)
        return
    if not is_multiple_of_tick(price, tick):
        raise OrderRejection(
            f"price {price} is not a multiple of tick size {tick}"
        )


def validate_limit(
    *,
    market: Market,
    side: Side,
    size_shares: float,
    price: float,
    order_type: OrderType,
    post_only: bool,
    expiration_ms: Optional[int],
    now_ms: int,
) -> None:
    if order_type not in (OrderType.GTC, OrderType.GTD):
        raise OrderRejection(f"limit order must be GTC or GTD, got {order_type}")
    if size_shares <= 0:
        raise OrderRejection(f"size must be positive, got {size_shares}")
    _validate_price_on_tick(price, market)

    if order_type == OrderType.GTD:
        if expiration_ms is None:
            raise OrderRejection("GTD requires expiration_ms")
        if expiration_ms < now_ms + GTD_MIN_LIFETIME_S * 1000:
            raise OrderRejection(
                f"GTD expiration must be ≥ now + {GTD_MIN_LIFETIME_S}s"
            )
    # post_only is fine for both GTC and GTD; matching engine handles cross-rejection.
    _ = post_only


def validate_market_buy(
    *,
    market: Market,
    notional_usdc: float,
    order_type: OrderType,
    slip_limit_price: Optional[float],
) -> None:
    if order_type not in (OrderType.FAK, OrderType.FOK):
        raise OrderRejection(f"market order must be FAK or FOK, got {order_type}")
    if notional_usdc <= 0:
        raise OrderRejection(f"notional must be positive, got {notional_usdc}")
    if slip_limit_price is not None:
        _validate_price_on_tick(slip_limit_price, market)


def validate_market_sell(
    *,
    market: Market,
    size_shares: float,
    order_type: OrderType,
    slip_limit_price: Optional[float],
) -> None:
    if order_type not in (OrderType.FAK, OrderType.FOK):
        raise OrderRejection(f"market order must be FAK or FOK, got {order_type}")
    if size_shares <= 0:
        raise OrderRejection(f"size must be positive, got {size_shares}")
    if slip_limit_price is not None:
        _validate_price_on_tick(slip_limit_price, market)

    if order_type == OrderType.FOK:
        # py-clob-client #121: FOK sell precision rules
        if _decimals_in(size_shares) > FOK_SELL_MAKER_AMOUNT_DECIMALS:
            raise OrderRejection(
                f"FOK SELL: size {size_shares} exceeds "
                f"{FOK_SELL_MAKER_AMOUNT_DECIMALS} decimal places"
            )
        if slip_limit_price is not None:
            taker_amt = round(size_shares * slip_limit_price, 10)
            if _decimals_in(taker_amt) > FOK_SELL_TAKER_AMOUNT_DECIMALS:
                raise OrderRejection(
                    f"FOK SELL: taker amount {taker_amt} exceeds "
                    f"{FOK_SELL_TAKER_AMOUNT_DECIMALS} decimal places"
                )
