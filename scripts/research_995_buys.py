#!/usr/bin/env python3
"""
Research: BUY transactions at price 0.995-0.999 across the last 5 IPL matches.

Source: Goldsky orderbook-subgraph (on-chain OrderFilled events) — captures
EVERY fill including maker-side limit buys that data-api /trades misses.

For each match's main (head-to-head) market, finds every fill where the
buyer-side price was in [0.995, 0.999], splits by winning vs losing outcome,
and ranks wallets by settlement profit (winning side: (1-p)*tokens; losing
side: -p*tokens).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import httpx

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
GOLDSKY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)
CTF_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"

# 5 most recent settled matches (Apr 25-27, 2026)
MATCHES = [
    "cricipl-del-roy-2026-04-27",
    "cricipl-che-guj-2026-04-26",
    "cricipl-luc-kol-2026-04-26",
    "cricipl-del-pun-2026-04-25",
    "cricipl-raj-sun-2026-04-25",
]

PRICE_LO = 0.995
PRICE_HI = 0.999


def fetch_event(slug: str) -> dict:
    r = httpx.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise RuntimeError(f"event not found: {slug}")
    return data[0]


def main_market(event: dict) -> dict:
    for m in event["markets"]:
        s = m.get("slug", "")
        if "toss-winner" in s or "completed-match" in s:
            continue
        return m
    raise RuntimeError(f"no main market in {event.get('slug')}")


def winner_loser(market: dict) -> tuple[str, str, list[str]]:
    outcomes = json.loads(market["outcomes"]) if isinstance(market["outcomes"], str) else market["outcomes"]
    prices = json.loads(market["outcomePrices"]) if isinstance(market["outcomePrices"], str) else market["outcomePrices"]
    winner = next((o for o, p in zip(outcomes, prices) if float(p) == 1.0), None)
    loser = next((o for o, p in zip(outcomes, prices) if float(p) == 0.0), None)
    if winner is None or loser is None:
        raise RuntimeError(f"could not resolve winner/loser: {outcomes} / {prices}")
    return winner, loser, outcomes


def parse_token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        return json.loads(raw)
    return list(raw or [])


def query_goldsky(field_name: str, token_ids: list[str], page_size: int = 1000) -> list[dict]:
    """Cursor-paginate orderFilledEvents by makerAssetId_in or takerAssetId_in."""
    events: list[dict] = []
    last_id = ""
    page = 0
    while True:
        id_filter = f', id_gt: "{last_id}"' if last_id else ""
        query = f"""{{
          orderFilledEvents(
            first: {page_size},
            orderBy: id, orderDirection: asc,
            where: {{ {field_name}: {json.dumps(token_ids)}{id_filter} }}
          ) {{
            id maker taker makerAssetId takerAssetId
            makerAmountFilled takerAmountFilled fee
            timestamp transactionHash orderHash
          }}
        }}"""
        try:
            r = httpx.post(GOLDSKY_ENDPOINT, json={"query": query}, timeout=60)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  goldsky page {page} error: {e}; retrying with page_size=500")
            time.sleep(1)
            try:
                q2 = query.replace(f"first: {page_size}", "first: 500")
                r = httpx.post(GOLDSKY_ENDPOINT, json={"query": q2}, timeout=60)
                r.raise_for_status()
                data = r.json()
            except Exception as e2:
                print(f"  goldsky retry failed: {e2}")
                break
        if "errors" in data:
            print(f"  goldsky errors: {data['errors']}")
            break
        batch = data.get("data", {}).get("orderFilledEvents", []) or []
        if not batch:
            break
        events.extend(batch)
        last_id = batch[-1]["id"]
        page += 1
        if len(batch) < page_size:
            break
        time.sleep(0.15)
    return events


def fetch_all_fills(token_ids: list[str]) -> list[dict]:
    seen: dict[str, dict] = {}
    for field in ["makerAssetId_in", "takerAssetId_in"]:
        for ev in query_goldsky(field, token_ids):
            seen[ev["id"]] = ev
    return list(seen.values())


def process_fills(events: list[dict], token_ids: list[str], outcome_names: list[str]) -> list[dict]:
    """Return one dict per fill with: ts, outcome, price, tokens, usdc, buyer, seller."""
    tok_to_outcome = dict(zip(token_ids, outcome_names))
    out: list[dict] = []
    for ev in events:
        maker = ev["maker"].lower()
        taker = ev["taker"].lower()
        maker_asset = ev["makerAssetId"]
        taker_asset = ev["takerAssetId"]
        maker_amt = int(ev["makerAmountFilled"])
        taker_amt = int(ev["takerAmountFilled"])

        if maker_asset in tok_to_outcome:
            # maker offered tokens (selling), taker paid USDC
            outcome = tok_to_outcome[maker_asset]
            tokens_raw = maker_amt
            usdc_raw = taker_amt
            seller, buyer = maker, taker
        elif taker_asset in tok_to_outcome:
            # taker offered tokens (selling), maker paid USDC
            outcome = tok_to_outcome[taker_asset]
            tokens_raw = taker_amt
            usdc_raw = maker_amt
            seller, buyer = taker, maker
        else:
            continue
        tokens = tokens_raw / 1e6
        usdc = usdc_raw / 1e6
        if tokens <= 0:
            continue
        price = usdc / tokens
        out.append({
            "id": ev["id"],
            "ts": int(ev["timestamp"]),
            "outcome": outcome,
            "price": round(price, 6),
            "tokens": tokens,
            "usdc": usdc,
            "buyer": buyer,
            "seller": seller,
            "tx_hash": ev["transactionHash"],
        })
    return out


# Per-wallet name lookup via data-api (best-effort, cached)
_NAME_CACHE: dict[str, str] = {}


def fetch_wallet_name(wallet: str, market_cid: str | None = None) -> str:
    if not wallet:
        return ""
    w = wallet.lower()
    if w in _NAME_CACHE:
        return _NAME_CACHE[w]
    try:
        params = {"user": w, "limit": 1}
        if market_cid:
            params["market"] = market_cid
        r = httpx.get(f"{DATA_API}/trades", params=params, timeout=15)
        if r.status_code == 200:
            d = r.json()
            if d and isinstance(d, list):
                name = d[0].get("name") or d[0].get("pseudonym") or ""
                _NAME_CACHE[w] = name
                return name
    except Exception:
        pass
    _NAME_CACHE[w] = ""
    return ""


def analyze(slug: str) -> dict:
    print(f"\n=== {slug} ===")
    event = fetch_event(slug)
    market = main_market(event)
    cid = market["conditionId"]
    token_ids = parse_token_ids(market)
    winner, loser, outcomes = winner_loser(market)
    print(f"  conditionId={cid}")
    print(f"  outcomes={outcomes}  winner={winner}")

    print("  fetching Goldsky orderFilledEvents...")
    raw = fetch_all_fills(token_ids)
    print(f"  raw events: {len(raw)}")
    fills = process_fills(raw, token_ids, outcomes)
    # Drop fills where buyer is the CTF exchange itself (shouldn't happen but safety)
    fills = [f for f in fills if f["buyer"] != CTF_EXCHANGE]
    print(f"  processed fills: {len(fills)}")

    in_band = [f for f in fills if PRICE_LO <= f["price"] <= PRICE_HI]
    print(f"  fills in [{PRICE_LO}, {PRICE_HI}]: {len(in_band)}")
    win_fills = [f for f in in_band if f["outcome"] == winner]
    lose_fills = [f for f in in_band if f["outcome"] == loser]
    print(f"    on winning side ({winner}): {len(win_fills)}")
    print(f"    on losing side ({loser}): {len(lose_fills)}")

    # Aggregate per buyer (winning side)
    win_by_wallet: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "tokens": 0.0, "notional": 0.0,
        "settle_profit": 0.0, "prices": [], "first_ts": 0, "last_ts": 0,
    })
    for f in win_fills:
        d = win_by_wallet[f["buyer"]]
        d["trades"] += 1
        d["tokens"] += f["tokens"]
        d["notional"] += f["usdc"]
        d["settle_profit"] += (1.0 - f["price"]) * f["tokens"]
        d["prices"].append(f["price"])
        if d["first_ts"] == 0 or f["ts"] < d["first_ts"]:
            d["first_ts"] = f["ts"]
        if f["ts"] > d["last_ts"]:
            d["last_ts"] = f["ts"]

    # Aggregate per buyer (losing side flag)
    lose_by_wallet: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "tokens": 0.0, "notional": 0.0, "loss": 0.0,
        "prices": [], "first_ts": 0,
    })
    for f in lose_fills:
        d = lose_by_wallet[f["buyer"]]
        d["trades"] += 1
        d["tokens"] += f["tokens"]
        d["notional"] += f["usdc"]
        d["loss"] += -f["price"] * f["tokens"]
        d["prices"].append(f["price"])
        if d["first_ts"] == 0 or f["ts"] < d["first_ts"]:
            d["first_ts"] = f["ts"]

    return {
        "match": slug,
        "winner": winner,
        "loser": loser,
        "conditionId": cid,
        "total_fills": len(fills),
        "in_band": len(in_band),
        "win_count": len(win_fills),
        "lose_count": len(lose_fills),
        "win_by_wallet": dict(win_by_wallet),
        "lose_by_wallet": dict(lose_by_wallet),
    }


def fmt_w(w: str) -> str:
    return w[:6] + ".." + w[-4:] if w and len(w) >= 10 else w


def render(results: list[dict], out_path: Path) -> str:
    L: list[str] = []
    L.append("# 99.5+ BUY Transaction Research — Last 5 IPL Matches\n")
    L.append(f"**Price band:** {PRICE_LO} ≤ price ≤ {PRICE_HI}  (every fill where the buyer's effective price was in this band — includes both maker and taker buyers)\n")
    L.append("**Source:** Goldsky `orderbook-subgraph` OrderFilled events. Buyer = wallet that received the YES token. Settlement profit = (1 − price) × tokens (winning side).\n")

    cross_win: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "tokens": 0.0, "notional": 0.0, "settle_profit": 0.0,
        "matches": set(), "name": "",
    })
    cross_lose: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "tokens": 0.0, "notional": 0.0, "loss": 0.0,
        "matches": set(), "name": "",
    })

    grand_count = 0
    grand_notional = 0.0
    grand_profit = 0.0
    grand_lose_count = 0
    grand_lose_loss = 0.0

    for r in results:
        L.append(f"\n## {r['match']}")
        L.append(f"- Winner: **{r['winner']}**  /  Loser: {r['loser']}")
        L.append(f"- Total fills (Goldsky): {r['total_fills']}")
        L.append(f"- Fills in [{PRICE_LO}, {PRICE_HI}]: **{r['in_band']}**")
        L.append(f"  - On winning side ({r['winner']}): **{r['win_count']}**")
        L.append(f"  - On losing side ({r['loser']}): **{r['lose_count']}**")
        m_notional = sum(d["notional"] for d in r["win_by_wallet"].values())
        m_profit = sum(d["settle_profit"] for d in r["win_by_wallet"].values())
        L.append(f"- Winning-side notional spent: **${m_notional:,.2f}**")
        L.append(f"- Winning-side total settlement profit: **${m_profit:,.2f}**")
        grand_count += r["in_band"]
        grand_notional += m_notional
        grand_profit += m_profit
        grand_lose_count += r["lose_count"]
        grand_lose_loss += sum(d["loss"] for d in r["lose_by_wallet"].values())

        # Top earners
        top = sorted(r["win_by_wallet"].items(), key=lambda kv: -kv[1]["settle_profit"])[:15]
        if top:
            L.append("\n  ### Top winning-side earners")
            L.append("  | # | Wallet | Name | Trades | Avg Px | Tokens | Notional ($) | Profit ($) |")
            L.append("  |---|---|---|---|---|---|---|---|")
            for i, (w, d) in enumerate(top, 1):
                avg = sum(d["prices"]) / len(d["prices"]) if d["prices"] else 0
                name = fetch_wallet_name(w, r["conditionId"])
                L.append(
                    f"  | {i} | `{fmt_w(w)}` | {name[:18]} | {d['trades']} | {avg:.4f} | {d['tokens']:,.0f} | {d['notional']:,.2f} | {d['settle_profit']:,.2f} |"
                )

        if r["lose_by_wallet"]:
            L.append("\n  ### ⚠️ Losing-side BUYS at 99.5+ (full loss)")
            L.append("  | Wallet | Name | Trades | Avg Px | Tokens | Notional ($) | Loss ($) |")
            L.append("  |---|---|---|---|---|---|---|")
            for w, d in sorted(r["lose_by_wallet"].items(), key=lambda kv: kv[1]["loss"]):
                avg = sum(d["prices"]) / len(d["prices"]) if d["prices"] else 0
                name = fetch_wallet_name(w, r["conditionId"])
                L.append(
                    f"  | `{fmt_w(w)}` | {name[:18]} | {d['trades']} | {avg:.4f} | {d['tokens']:,.0f} | {d['notional']:,.2f} | {d['loss']:,.2f} |"
                )

        # Aggregate cross-match
        for w, d in r["win_by_wallet"].items():
            cd = cross_win[w]
            cd["trades"] += d["trades"]
            cd["tokens"] += d["tokens"]
            cd["notional"] += d["notional"]
            cd["settle_profit"] += d["settle_profit"]
            cd["matches"].add(r["match"])
        for w, d in r["lose_by_wallet"].items():
            cd = cross_lose[w]
            cd["trades"] += d["trades"]
            cd["tokens"] += d["tokens"]
            cd["notional"] += d["notional"]
            cd["loss"] += d["loss"]
            cd["matches"].add(r["match"])

    L.append("\n---\n## CROSS-MATCH SUMMARY")
    L.append(f"- Total qualifying BUY fills (5 matches): **{grand_count}**")
    L.append(f"  - Winning side: **{grand_count - grand_lose_count}**")
    L.append(f"  - Losing side: **{grand_lose_count}**")
    L.append(f"- Total notional spent (winning side): **${grand_notional:,.2f}**")
    L.append(f"- Total settlement profit (winning side): **${grand_profit:,.2f}**")
    if grand_lose_count:
        L.append(f"- Total losing-side notional: **${sum(d['notional'] for d in cross_lose.values()):,.2f}**  (full loss: ${grand_lose_loss:,.2f})")
    L.append(f"- Unique wallets — winning side: **{len(cross_win)}**")
    L.append(f"- Unique wallets — losing side: **{len(cross_lose)}**")

    L.append("\n### Top 25 cross-match winning-side earners")
    L.append("| # | Wallet | Name | # Matches | Trades | Tokens | Notional ($) | Profit ($) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, (w, d) in enumerate(sorted(cross_win.items(), key=lambda kv: -kv[1]["settle_profit"])[:25], 1):
        name = fetch_wallet_name(w)
        L.append(
            f"| {i} | `{fmt_w(w)}` | {name[:18]} | {len(d['matches'])} | {d['trades']} | {d['tokens']:,.0f} | {d['notional']:,.2f} | {d['settle_profit']:,.2f} |"
        )

    if cross_lose:
        L.append("\n### ⚠️ Cross-match losing-side flag")
        L.append("| Wallet | Name | # Matches | Trades | Tokens | Notional ($) | Loss ($) |")
        L.append("|---|---|---|---|---|---|---|")
        for w, d in sorted(cross_lose.items(), key=lambda kv: kv[1]["loss"])[:25]:
            name = fetch_wallet_name(w)
            L.append(
                f"| `{fmt_w(w)}` | {name[:18]} | {len(d['matches'])} | {d['trades']} | {d['tokens']:,.0f} | {d['notional']:,.2f} | {d['loss']:,.2f} |"
            )

    text = "\n".join(L)
    out_path.write_text(text)
    return text


def main():
    results = [analyze(s) for s in MATCHES]
    out = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/research_995_buys_report.md")
    text = render(results, out)
    print(f"\nReport written to {out}\n")
    print(text[-4000:])


if __name__ == "__main__":
    main()
