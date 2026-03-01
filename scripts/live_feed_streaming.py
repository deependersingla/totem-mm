#!/usr/bin/env python3
"""
Live feed – STREAMING. Betfair WebSocket stream + Polymarket fast poll (0.5s).
Writes one row per update to data/live_odds_streaming.txt (same visual format as polling).

Uses .env: BETFAIR_MARKET_IDS, TOKEN_MAP, TEAM_A, TEAM_B; Betfair streaming credentials
(USERNAME, PASSWORD, APP_KEY, CERTS or CERT_FILE).
"""

import os
import queue
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, SCRIPT_DIR)

import dotenv
dotenv.load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from live_feed_common import (
    betfair_odds_to_probs,
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
STREAMING_FILE = os.path.join(DATA_DIR, "live_odds_streaming.txt")
POLY_POLL_INTERVAL = 0.5


def _fmt(x: float | None) -> str:
    if x is None:
        return ""
    return f"{x:.4f}"


def _build_visual(state: dict, lock: threading.Lock, selection_ids_ordered: list, team_a: str, team_b: str) -> str:
    """Build one visual line from shared state (bf_probs, poly_parts)."""
    with lock:
        bf_probs = state.get("bf_probs") or {}
        poly_parts = state.get("poly_parts") or [None] * 8
    row = [ist_now()]
    for sid in selection_ids_ordered:
        p = bf_probs.get(sid) or {}
        row.append(_fmt(p.get("back_pct")))
        row.append(_fmt(p.get("lay_pct")))
        row.append(_fmt(p.get("last_pct")))
    for i in range(8):
        v = poly_parts[i] if i < len(poly_parts) else None
        row.append(_fmt(v))
    if len(row) >= 15:
        ist, ab, al, alt, bb, bl, blt, a_pb, a_pa, a_plt, a_pr, b_pb, b_pa, b_plt, b_pr = row[:15]
        a_bf = f"{ab or '-'}/{al or '-'}/{alt or '-'}"
        b_bf = f"{bb or '-'}/{bl or '-'}/{blt or '-'}"
        a_poly = f"{a_pb or '-'}/{a_pa or '-'}/{a_plt or '-'}/{a_pr or '-'}"
        b_poly = f"{b_pb or '-'}/{b_pa or '-'}/{b_plt or '-'}/{b_pr or '-'}"
        return f"{ist} | {team_a} BF: {a_bf:28} | {team_b} BF: {b_bf:28} | {team_a} Poly: {a_poly:32} | {team_b} Poly: {b_poly}"
    return "\t".join(str(x) for x in row)


def _market_book_to_odds(market) -> dict:
    """Convert betfairlightweight MarketBook to selection_id -> {back, lay, last_traded}."""
    out = {}
    for runner in getattr(market, "runners", []) or []:
        sid = getattr(runner, "selection_id", None)
        if sid is None:
            continue
        atb = getattr(runner, "available_to_back", None) or (
            getattr(getattr(runner, "ex", None), "available_to_back", None)
        )
        atl = getattr(runner, "available_to_lay", None) or (
            getattr(getattr(runner, "ex", None), "available_to_lay", None)
        )
        back = atb[0].price if atb and len(atb) > 0 else None
        lay = atl[0].price if atl and len(atl) > 0 else None
        last = getattr(runner, "last_price_traded", None)
        out[sid] = {"back": back, "lay": lay, "last_traded": last}
    return out


def _make_listener_class(market_ids: list, selection_ids_ordered: list, team_a: str, team_b: str, state: dict, lock: threading.Lock, write_queue: queue.Queue):
    """Build a StreamListener subclass that shares state and queue (library sets .stream on this instance)."""
    from betfairlightweight.streaming import StreamListener

    class LiveFeedStreamListener(StreamListener):
        def __init__(self):
            super().__init__(output_queue=queue.Queue(), max_latency=0.5, lightweight=False)
            self._market_ids = market_ids
            self._selection_ids_ordered = selection_ids_ordered
            self._team_a = team_a
            self._team_b = team_b
            self._state = state
            self._lock = lock
            self._write_queue = write_queue

        def on_data(self, raw_data: str):
            result = super().on_data(raw_data)
            if result is False:
                return False
            if not getattr(self, "stream", None) or getattr(self, "stream_type", None) != "marketSubscription":
                return None
            try:
                market_books = self.stream.snap(self._market_ids)
                if not market_books:
                    return None
                for market in market_books:
                    if market is None:
                        continue
                    odds = _market_book_to_odds(market)
                    if not odds:
                        continue
                    probs = betfair_odds_to_probs(odds)
                    with self._lock:
                        if "bf_probs" not in self._state:
                            self._state["bf_probs"] = {}
                        self._state["bf_probs"].update(probs)
                    visual = _build_visual(self._state, self._lock, self._selection_ids_ordered, self._team_a, self._team_b)
                    try:
                        self._write_queue.put(("betfair", visual))
                    except Exception:
                        pass
            except Exception:
                pass
            return None

    return LiveFeedStreamListener


def run_poly_poll(state: dict, lock: threading.Lock, write_queue: queue.Queue, sel_to_token: dict, selection_ids_ordered: list, team_a: str, team_b: str):
    """Poll Polymarket every POLY_POLL_INTERVAL and push ('polymarket', visual) to queue."""
    while True:
        try:
            parts = []
            for sid in selection_ids_ordered:
                tid = sel_to_token.get(sid)
                if not tid:
                    parts.extend([None] * 4)
                    continue
                try:
                    book = fetch_poly_book(tid)
                    bid, ask, lt = poly_book_to_probs(book)
                    pr = poly_prices_last(fetch_poly_prices(tid))
                    parts.append(bid * 100.0 if bid is not None else None)
                    parts.append(ask * 100.0 if ask is not None else None)
                    parts.append(lt * 100.0 if lt is not None else None)
                    parts.append(pr * 100.0 if pr is not None else None)
                except Exception:
                    parts.extend([None] * 4)
            with lock:
                state["poly_parts"] = parts
            visual = _build_visual(state, lock, selection_ids_ordered, team_a, team_b)
            try:
                write_queue.put(("polymarket", visual))
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(POLY_POLL_INTERVAL)


def run_writer(write_queue: queue.Queue, filepath: str, header_line: str, sep_line: str):
    """Consume (source, line) from queue; write to file and print. Only handles source in ('betfair', 'polymarket')."""
    os.makedirs(DATA_DIR, exist_ok=True)
    # Write header once if file is missing or empty (open/close so it's committed)
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(header_line + "\n")
            f.write(sep_line + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    while True:
        try:
            item = write_queue.get()
            if item is None:
                break
            source = str(item[0]) if isinstance(item, (tuple, list)) and len(item) >= 1 else None
            if source not in ("betfair", "polymarket"):
                continue
            line = item[1] if isinstance(item, (tuple, list)) and len(item) >= 2 else str(item)
            line = str(line)
            wrote = False
            try:
                # Open, write one line, close so each line is committed to disk
                with open(filepath, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                wrote = True
            except Exception:
                pass
            if wrote:
                print(line)
        except Exception:
            pass


def main():
    token_map = get_token_map()
    market_ids = get_market_ids()
    if not token_map or not market_ids:
        print("Set BETFAIR_MARKET_IDS and TOKEN_MAP in .env", file=sys.stderr)
        sys.exit(1)

    username = os.environ.get("BETFAIR_USERNAME", "").strip()
    password = os.environ.get("BETFAIR_PASSWORD", "").strip()
    app_key = (os.environ.get("BETFAIR_APP_KEY") or "").strip()
    if not username or not password or not app_key:
        print("Set BETFAIR_USERNAME, BETFAIR_PASSWORD, BETFAIR_APP_KEY for streaming.", file=sys.stderr)
        sys.exit(1)

    team_a, team_b = get_team_labels()
    sel_to_token = {sel_id: tid for tid, sel_id in token_map.items()}
    selection_ids_ordered = sorted(sel_to_token.keys())

    header_line = f"IST     | {team_a} Betfair (back/lay/last)       | {team_b} Betfair (back/lay/last)      | {team_a} Poly (bid/ask/lt/price)           | {team_b} Poly (bid/ask/lt/price)"
    sep_line = "-" * 130

    state = {"bf_probs": {}, "poly_parts": [None] * 8}
    lock = threading.Lock()
    write_queue = queue.Queue()

    writer = threading.Thread(target=run_writer, args=(write_queue, STREAMING_FILE, header_line, sep_line), daemon=True)
    writer.start()

    poly_thread = threading.Thread(
        target=run_poly_poll,
        args=(state, lock, write_queue, sel_to_token, selection_ids_ordered, team_a, team_b),
        daemon=True,
    )
    poly_thread.start()

    print("Streaming →", STREAMING_FILE)
    print(header_line)
    print(sep_line)

    import betfairlightweight
    from betfairlightweight import filters

    certs = os.environ.get("BETFAIR_CERTS", "").strip() or "certs"
    cert_file = os.environ.get("BETFAIR_CERT_FILE", "").strip()
    client_kw = {"username": username, "password": password, "app_key": app_key}
    if cert_file:
        client_kw["cert_files"] = cert_file
    else:
        client_kw["certs"] = certs

    trading = betfairlightweight.APIClient(**client_kw)
    trading.login()

    listener_class = _make_listener_class(
        market_ids=market_ids,
        selection_ids_ordered=selection_ids_ordered,
        team_a=team_a,
        team_b=team_b,
        state=state,
        lock=lock,
        write_queue=write_queue,
    )
    listener = listener_class()
    stream = trading.streaming.create_stream(listener=listener)
    market_filter = filters.streaming_market_filter(market_ids=market_ids)
    market_data_filter = filters.streaming_market_data_filter(
        fields=["EX_BEST_OFFERS", "EX_MARKET_DEF", "EX_TRADED"],
        ladder_levels=1,
    )
    stream.subscribe_to_markets(market_filter=market_filter, market_data_filter=market_data_filter)
    stream.start()


if __name__ == "__main__":
    main()
