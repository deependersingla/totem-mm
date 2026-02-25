"""
Shared helpers for live_feed_polling and live_feed_streaming.
IST time, Betfair/Poly prob extraction, Poly HTTP.
"""

import json
import os
import urllib.request
from datetime import datetime
from typing import Any

import pytz

IST = pytz.timezone("Asia/Kolkata")


def ist_now() -> str:
    """Current time in IST as HH:MM:SS."""
    return datetime.now(IST).strftime("%H:%M:%S")


def betfair_odds_to_probs(odds: dict[int, dict]) -> dict[int, dict[str, float | None]]:
    """Convert feed snapshot (selection_id -> {back, lay, last_traded}) to back_pct, lay_pct, last_pct."""
    out: dict[int, dict[str, float | None]] = {}
    for sid, o in odds.items():
        back = o.get("back")
        lay = o.get("lay")
        last = o.get("last_traded")
        out[sid] = {
            "back_pct": (100.0 / back) if back else None,
            "lay_pct": (100.0 / lay) if lay else None,
            "last_pct": (100.0 / last) if last else None,
        }
    return out


def betfair_book_to_probs(market_book: dict) -> dict[int, dict[str, float | None]]:
    """
    From listMarketBook result (one market), return
    selection_id -> { "back_pct", "lay_pct", "last_pct" } (percent 0-100 or None).
    """
    out: dict[int, dict[str, float | None]] = {}
    for r in market_book.get("runners") or []:
        sid = r.get("selectionId")
        if sid is None:
            continue
        ex = r.get("ex") or {}
        atb = ex.get("availableToBack") or []
        atl = ex.get("availableToLay") or []
        back = atb[0]["price"] if atb else None
        lay = atl[0]["price"] if atl else None
        last = r.get("lastPriceTraded")
        out[sid] = {
            "back_pct": (100.0 / back) if back else None,
            "lay_pct": (100.0 / lay) if lay else None,
            "last_pct": (100.0 / last) if last else None,
        }
    return out


def poly_book_to_probs(book: dict) -> tuple[float | None, float | None, float | None]:
    """
    From CLOB /book response: (best_bid, best_ask, last_trade_price) as 0-1 price.
    best_bid = max of bids price, best_ask = min of asks price.
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max((float(b["price"]) for b in bids), default=None) if bids else None
    best_ask = min((float(a["price"]) for a in asks), default=None) if asks else None
    ltp = book.get("last_trade_price")
    last = float(ltp) if ltp is not None else None
    return best_bid, best_ask, last


def poly_prices_last(prices_resp: dict) -> float | None:
    """From prices-history response, return last price (0-1) or None."""
    hist = prices_resp.get("history") or []
    if not hist:
        return None
    return float(hist[-1]["p"])


def poly_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


def fetch_poly_book(token_id: str) -> dict:
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    req = urllib.request.Request(url, headers=poly_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def fetch_poly_prices(token_id: str) -> dict:
    url = f"https://clob.polymarket.com/prices-history?market={token_id}&interval=1h&fidelity=1"
    req = urllib.request.Request(url, headers=poly_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get_team_labels() -> tuple[str, str]:
    """Labels for the two runners (first, second by selection_id). Defaults: AUS, IND."""
    a = (os.environ.get("TEAM_A") or "").strip() or "AUS"
    b = (os.environ.get("TEAM_B") or "").strip() or "IND"
    return a, b


def get_token_map() -> dict[str, int]:
    raw = os.environ.get("TOKEN_MAP", "{}")
    return json.loads(raw) if raw else {}


def get_market_ids() -> list[str]:
    raw = os.environ.get("BETFAIR_MARKET_IDS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]
