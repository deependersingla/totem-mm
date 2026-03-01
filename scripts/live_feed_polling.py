#!/usr/bin/env python3
"""
Live feed – POLLING. Writes one row per second to data/live_odds_polling.txt.

Columns: IST, Betfair probs (back, lay, last) per runner, Polymarket (bid, ask, last_trade, pricing) per token.
Uses .env: BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, TEAM_B (display labels), plus Betfair credentials.
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)

import dotenv
dotenv.load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from live_feed_common import (
    betfair_book_to_probs,
    fetch_poly_book,
    fetch_poly_prices,
    get_market_ids,
    get_team_labels,
    get_token_map,
    ist_now,
    poly_book_to_probs,
    poly_prices_last,
)

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
POLLING_FILE = os.path.join(DATA_DIR, "live_odds_polling.txt")
POLL_INTERVAL = 1.0


def _fmt(x: float | None) -> str:
    if x is None:
        return ""
    return f"{x:.4f}"


def fetch_betfair_book():
    from connectors.betfair.client import BetfairClient
    market_ids = get_market_ids()
    if not market_ids:
        return None
    client = BetfairClient()
    params = {
        "marketIds": market_ids,
        "priceProjection": {"priceData": ["EX_BEST_OFFERS", "EX_TRADED"]},
    }
    resp = client.call("SportsAPING/v1.0/listMarketBook", params)
    result = resp.get("result") or []
    return result[0] if result else None


def main():
    token_map = get_token_map()
    market_ids = get_market_ids()
    if not token_map or not market_ids:
        print("Set BETFAIR_MARKET_IDS and TOKEN_MAP in .env", file=sys.stderr)
        sys.exit(1)

    # Order: by selection_id (first = team_a, second = team_b)
    team_a, team_b = get_team_labels()
    sel_to_token = {sel_id: tid for tid, sel_id in token_map.items()}
    selection_ids_ordered = sorted(sel_to_token.keys())

    os.makedirs(DATA_DIR, exist_ok=True)
    header_line = f"IST     | {team_a} Betfair (back/lay/last)       | {team_b} Betfair (back/lay/last)      | {team_a} Poly (bid/ask/lt/price)           | {team_b} Poly (bid/ask/lt/price)"
    sep_line = "-" * 130
    with open(POLLING_FILE, "a") as f:
        if os.path.getsize(POLLING_FILE) == 0:
            f.write(header_line + "\n")
            f.write(sep_line + "\n")
            f.flush()

    print("Polling every", POLL_INTERVAL, "s →", POLLING_FILE)
    print(header_line)
    print(sep_line)
    while True:
        t0 = time.perf_counter()
        row_parts = [ist_now()]

        bf = fetch_betfair_book()
        if bf:
            probs = betfair_book_to_probs(bf)
            for sid in selection_ids_ordered:
                p = probs.get(sid) or {}
                row_parts.append(_fmt(p.get("back_pct")))
                row_parts.append(_fmt(p.get("lay_pct")))
                row_parts.append(_fmt(p.get("last_pct")))
        else:
            for _ in selection_ids_ordered:
                row_parts.extend(["", "", ""])

        for tid in [sel_to_token[sid] for sid in selection_ids_ordered]:
            try:
                book = fetch_poly_book(tid)
                bid, ask, lt = poly_book_to_probs(book)
                # store as % 0-100
                row_parts.append(_fmt(bid * 100.0) if bid is not None else "")
                row_parts.append(_fmt(ask * 100.0) if ask is not None else "")
                row_parts.append(_fmt(lt * 100.0) if lt is not None else "")
                pr = poly_prices_last(fetch_poly_prices(tid))
                row_parts.append(_fmt(pr * 100.0) if pr is not None else "")
            except Exception:
                row_parts.extend(["", "", "", ""])

        # Build visual line (same as terminal) and write to file + print
        if len(row_parts) >= 15:
            ist, ab, al, alt, bb, bl, blt, a_pb, a_pa, a_plt, a_pr, b_pb, b_pa, b_plt, b_pr = row_parts[:15]
            a_bf = f"{ab or '-'}/{al or '-'}/{alt or '-'}"
            b_bf = f"{bb or '-'}/{bl or '-'}/{blt or '-'}"
            a_poly = f"{a_pb or '-'}/{a_pa or '-'}/{a_plt or '-'}/{a_pr or '-'}"
            b_poly = f"{b_pb or '-'}/{b_pa or '-'}/{b_plt or '-'}/{b_pr or '-'}"
            visual = f"{ist} | {team_a} BF: {a_bf:28} | {team_b} BF: {b_bf:28} | {team_a} Poly: {a_poly:32} | {team_b} Poly: {b_poly}"
        else:
            visual = "\t".join(row_parts)
        with open(POLLING_FILE, "a") as f:
            f.write(visual + "\n")
            f.flush()
        print(visual)

        elapsed = time.perf_counter() - t0
        sleep = max(0.0, POLL_INTERVAL - elapsed)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()
