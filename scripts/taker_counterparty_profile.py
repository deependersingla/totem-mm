#!/usr/bin/env python3
"""
Profile the counterparties (takers) that recurrently lift 0xef51eb's resting
maker offers on Polymarket IPL cricket markets.

Inputs:
  captures/edge_cache/*_0xef51eb.json   -- cached Goldsky orderFilledEvents
  captures/match_capture_*.db            -- one sqlite db per match with match_meta

Outputs (stdout):
  - Top 20 counterparty table (address, n_fills, n_matches, notional_$, size
    distribution, buy%/sell%, classification)
  - Top-5 broader Goldsky activity summary
  - Headline retail vs algo share of 0xef51eb's maker volume

Goldsky queries for broader activity are cached under
captures/edge_cache/taker_broader/<addr>.json so reruns are fast.
"""

import glob
import json
import os
import statistics
import sys
import time
from collections import defaultdict

import requests

TARGET = "0xef51ebb7ed5c84e5049fc76e1ae4db3b5799c0d3".lower()
CTF_AGG = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e".lower()

ROOT = "/Users/sobhagyaxd/DeepWork/totem-mm"
EDGE_CACHE = f"{ROOT}/captures/edge_cache"
BROADER_CACHE = f"{EDGE_CACHE}/taker_broader"
os.makedirs(BROADER_CACHE, exist_ok=True)

GOLDSKY = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn"
)


def gql(q, timeout=60):
    for i in range(4):
        try:
            r = requests.post(GOLDSKY, json={"query": q}, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if "errors" not in j:
                return j.get("data", {})
            print(f"  gql errors: {j['errors']}", file=sys.stderr)
        except Exception as e:
            print(f"  gql retry ({i}): {e}", file=sys.stderr)
            time.sleep(1 + i)
    return {}


def load_match_meta():
    """Return dict slug -> (token_ids_set, outcome_names)."""
    out = {}
    import sqlite3

    for db in sorted(glob.glob(f"{ROOT}/captures/match_capture_*.db")):
        if db.endswith(("-shm", "-wal")):
            continue
        try:
            c = sqlite3.connect(db)
            meta = dict(c.execute("SELECT key,value FROM match_meta").fetchall())
            c.close()
        except Exception:
            continue
        slug = meta.get("slug")
        if not slug:
            continue
        tok = set(json.loads(meta.get("token_ids", "[]")))
        outs = json.loads(meta.get("outcome_names", "[]"))
        out[slug] = (tok, outs)
    return out


def load_cache_fills():
    """
    Walk edge_cache files, dedupe by fill id across files.
    Returns list of fills augmented with 'slug' and a 'match_key'.
    """
    seen = {}
    for fp in sorted(glob.glob(f"{EDGE_CACHE}/*_0xef51eb.json")):
        d = json.load(open(fp))
        slug = d.get("slug")
        for f in d.get("fills", []):
            fid = f.get("id")
            if not fid or fid in seen:
                continue
            f = dict(f)
            f["slug"] = slug
            seen[fid] = f
    return list(seen.values())


def notional_and_side(fill, target_tokens):
    """
    Returns (notional_usd, side) where side is 'TAKER_BUY_TOKENS' if the
    taker received tokens (i.e. maker sold tokens; taker paid USDC), or
    'TAKER_SELL_TOKENS' if the taker delivered tokens (maker bought tokens;
    taker received USDC). target_tokens is the set of token ids for this match.
    """
    maker_asset = fill.get("makerAssetId")
    taker_asset = fill.get("takerAssetId")
    maker_amt = int(fill.get("makerAmountFilled", "0"))
    taker_amt = int(fill.get("takerAmountFilled", "0"))

    # USDC side is the one whose asset id is "0" (the collateral). The other
    # side should be an outcome token id (in target_tokens for our match).
    if maker_asset == "0":
        # Maker paid USDC; taker delivered tokens (taker SOLD tokens)
        usd = maker_amt / 1e6
        side = "TAKER_SELL_TOKENS"
    elif taker_asset == "0":
        # Taker paid USDC; maker delivered tokens (taker BOUGHT tokens)
        usd = taker_amt / 1e6
        side = "TAKER_BUY_TOKENS"
    else:
        # neither side is USDC — shouldn't happen on a binary market; fall back
        usd = min(maker_amt, taker_amt) / 1e6
        side = "UNKNOWN"
    return usd, side


def classify(stats):
    n_fills = stats["n_fills"]
    n_matches = stats["n_matches"]
    notional = stats["notional"]
    p90 = stats["p90"]
    both_sides = stats["buy_side_pct"] > 10 and stats["sell_side_pct"] > 10
    one_sided = stats["buy_side_pct"] > 90 or stats["sell_side_pct"] > 90

    if n_matches >= 7 and n_fills >= 100 and both_sides:
        return "ALGO/PRO"
    if n_matches >= 4 and n_fills >= 20:
        return "SEMI-ALGO"
    if n_matches <= 3 and n_fills <= 10 and notional < 10000 and p90 < 500 and one_sided:
        return "RETAIL"
    if n_fills <= 15 and notional < 5000 and p90 < 1000:
        return "RETAIL"
    return "SEMI-ALGO"


def fetch_broader_activity(addr, max_events=1000):
    """Fetch up to max_events orderFilledEvents where taker=addr, any market."""
    cache_path = f"{BROADER_CACHE}/{addr}.json"
    if os.path.exists(cache_path):
        try:
            return json.load(open(cache_path))
        except Exception:
            pass

    out = {}
    last = ""
    page = 1000 if max_events > 1000 else max_events
    pages = 0
    while pages * 1000 < max_events:
        cur = f', id_gt: "{last}"' if last else ""
        q = f"""{{
          orderFilledEvents(
            first: {page}, orderBy: id, orderDirection: asc,
            where: {{ taker: "{addr}" {cur} }}
          ) {{
            id maker taker makerAssetId takerAssetId
            makerAmountFilled takerAmountFilled timestamp
            transactionHash orderHash
          }}
        }}"""
        d = gql(q)
        b = (d or {}).get("orderFilledEvents", [])
        if not b:
            break
        for ev in b:
            out[ev["id"]] = ev
        last = b[-1]["id"]
        pages += 1
        if len(b) < page:
            break
        time.sleep(0.15)
    events = list(out.values())
    json.dump(events, open(cache_path, "w"))
    return events


def main():
    meta = load_match_meta()
    all_fills = load_cache_fills()

    # Keep only maker=TARGET, exclude CTF aggregator taker
    lifted = [
        f for f in all_fills
        if f.get("maker", "").lower() == TARGET
        and f.get("taker", "").lower() != CTF_AGG
    ]

    # Also tally total maker volume (incl. CTF aggregator) for denominator
    tot_maker_vol = 0.0
    for f in all_fills:
        if f.get("maker", "").lower() != TARGET:
            continue
        slug = f.get("slug")
        toks, _ = meta.get(slug, (set(), []))
        usd, _ = notional_and_side(f, toks)
        tot_maker_vol += usd

    # Non-CTF addressable volume (what we can attribute to actual counterparties)
    addressable_vol = 0.0

    per_addr = defaultdict(lambda: {
        "fills": [],
        "matches": set(),
        "notional": 0.0,
        "buy_token": 0,   # taker bought tokens (hit maker sell)
        "sell_token": 0,  # taker sold tokens  (hit maker buy)
        "sizes": [],
        "timestamps": [],
        "side_by_slug": defaultdict(lambda: [0, 0]),
    })

    for f in lifted:
        addr = f["taker"].lower()
        slug = f["slug"]
        toks, _ = meta.get(slug, (set(), []))
        usd, side = notional_and_side(f, toks)
        s = per_addr[addr]
        s["fills"].append(f)
        s["matches"].add(slug)
        s["notional"] += usd
        s["sizes"].append(usd)
        s["timestamps"].append(int(f.get("timestamp", "0")))
        if side == "TAKER_BUY_TOKENS":
            s["buy_token"] += 1
            s["side_by_slug"][slug][0] += 1
        elif side == "TAKER_SELL_TOKENS":
            s["sell_token"] += 1
            s["side_by_slug"][slug][1] += 1
        addressable_vol += usd

    rows = []
    for addr, s in per_addr.items():
        n = len(s["fills"])
        sizes = sorted(s["sizes"])
        p50 = statistics.median(sizes) if sizes else 0
        p90 = sizes[int(0.9 * (len(sizes) - 1))] if sizes else 0
        buy = s["buy_token"]
        sell = s["sell_token"]
        tot = buy + sell if (buy + sell) else 1
        stats = {
            "addr": addr,
            "n_fills": n,
            "n_matches": len(s["matches"]),
            "notional": s["notional"],
            "p50": p50,
            "p90": p90,
            "max": max(sizes) if sizes else 0,
            "buy_side_pct": 100 * buy / tot,
            "sell_side_pct": 100 * sell / tot,
        }
        stats["classification"] = classify(stats)
        rows.append(stats)

    rows.sort(key=lambda r: -r["notional"])

    print("=" * 120)
    print(f"0xef51eb maker offers lifted by non-CTF counterparties")
    print(f"  Total maker volume (incl. CTF aggregator): ${tot_maker_vol:,.0f}")
    print(f"  Addressable (non-CTF) volume:              ${addressable_vol:,.0f}"
          f"  ({100*addressable_vol/tot_maker_vol:.1f}% of total)")
    print(f"  Unique non-CTF counterparties:             {len(rows)}")
    print(f"  Fills covered:                             {sum(r['n_fills'] for r in rows)}")
    print("=" * 120)

    # TOP 20
    hdr = ("rank addr                                         n_fills n_mch "
           "notional_$   p50    p90    max      buy%  sell%  class")
    print(hdr)
    print("-" * len(hdr))
    for i, r in enumerate(rows[:20], 1):
        print(f"{i:>3} {r['addr']} {r['n_fills']:>7} {r['n_matches']:>5} "
              f"{r['notional']:>11,.0f} {r['p50']:>6,.0f} {r['p90']:>6,.0f} "
              f"{r['max']:>7,.0f}  {r['buy_side_pct']:>4.0f}  {r['sell_side_pct']:>4.0f}  "
              f"{r['classification']}")

    # Concentration
    top20_v = sum(r["notional"] for r in rows[:20])
    top5_v = sum(r["notional"] for r in rows[:5])
    print(f"\nTop 5  share of addressable: {100*top5_v/addressable_vol:>5.1f}%  (${top5_v:,.0f})")
    print(f"Top 20 share of addressable: {100*top20_v/addressable_vol:>5.1f}%  (${top20_v:,.0f})")

    # Totals by class
    print("\nBy classification (scope: ALL non-CTF counterparties):")
    cls_tot = defaultdict(lambda: [0, 0.0])  # count, notional
    for r in rows:
        cls_tot[r["classification"]][0] += 1
        cls_tot[r["classification"]][1] += r["notional"]
    for cls, (n, v) in sorted(cls_tot.items(), key=lambda x: -x[1][1]):
        share_total = 100 * v / tot_maker_vol
        share_addr = 100 * v / addressable_vol
        print(f"  {cls:<10} wallets={n:<5} notional=${v:>10,.0f}  "
              f"{share_addr:>5.1f}% of addressable  {share_total:>5.1f}% of total maker vol")

    # TOP 5 broader footprint
    print("\n" + "=" * 120)
    print("Top 5 by notional — broader Goldsky footprint (as taker, up to 1000 events)")
    print("=" * 120)

    top5 = rows[:5]
    all_token_ids = set()
    for toks, _ in meta.values():
        all_token_ids |= toks

    for r in top5:
        addr = r["addr"]
        print(f"\n--- {addr}  (cache: {r['n_fills']} fills, "
              f"${r['notional']:,.0f}, {r['classification']})")
        try:
            evs = fetch_broader_activity(addr, max_events=1000)
        except Exception as e:
            print(f"   broader fetch failed: {e}")
            continue
        n_ev = len(evs)
        if n_ev == 0:
            print("   no events returned")
            continue

        # Bucket by counterparty market (use makerAssetId or takerAssetId that isn't "0")
        asset_counts = defaultdict(int)
        asset_usd = defaultdict(float)
        makers = defaultdict(int)
        in_cache_cricket_usd = 0.0
        other_usd = 0.0
        ts_min = min(int(e["timestamp"]) for e in evs)
        ts_max = max(int(e["timestamp"]) for e in evs)
        for e in evs:
            ma = e.get("makerAssetId")
            ta = e.get("takerAssetId")
            mamt = int(e.get("makerAmountFilled", "0"))
            tamt = int(e.get("takerAmountFilled", "0"))
            if ma == "0":
                usd = mamt / 1e6
            elif ta == "0":
                usd = tamt / 1e6
            else:
                usd = min(mamt, tamt) / 1e6
            tok = ma if ma != "0" else ta
            asset_counts[tok] += 1
            asset_usd[tok] += usd
            makers[e.get("maker", "").lower()] += 1
            if tok in all_token_ids:
                in_cache_cricket_usd += usd
            else:
                other_usd += usd

        total_usd = in_cache_cricket_usd + other_usd
        cricket_pct = 100 * in_cache_cricket_usd / total_usd if total_usd else 0
        print(f"   events fetched: {n_ev}")
        print(f"   time window: {ts_min} -> {ts_max}  "
              f"(~{(ts_max-ts_min)/86400:.1f} days)")
        print(f"   distinct outcome tokens traded: {len(asset_counts)}")
        print(f"   volume inside our cached IPL matches: ${in_cache_cricket_usd:,.0f}  "
              f"({cricket_pct:.0f}% of all)")
        print(f"   volume outside those matches:         ${other_usd:,.0f}")
        print(f"   top counterparties (as maker to this taker):")
        for m, c in sorted(makers.items(), key=lambda x: -x[1])[:5]:
            tag = " <-- 0xef51eb" if m == TARGET else ""
            print(f"      {m} {c:>5} fills{tag}")

    # Headline
    retail_v = cls_tot.get("RETAIL", [0, 0.0])[1]
    semi_v = cls_tot.get("SEMI-ALGO", [0, 0.0])[1]
    algo_v = cls_tot.get("ALGO/PRO", [0, 0.0])[1]
    print("\n" + "=" * 120)
    print("HEADLINE")
    print("=" * 120)
    def pct(v): return 100 * v / addressable_vol if addressable_vol else 0
    print(f"  Of 0xef51eb's addressable maker volume (${addressable_vol:,.0f}):")
    print(f"    RETAIL     ${retail_v:>10,.0f}  {pct(retail_v):>5.1f}%")
    print(f"    SEMI-ALGO  ${semi_v:>10,.0f}  {pct(semi_v):>5.1f}%")
    print(f"    ALGO/PRO   ${algo_v:>10,.0f}  {pct(algo_v):>5.1f}%")
    if rows:
        top = rows[0]
        print(f"  Biggest single counterparty: {top['addr']} "
              f"({top['n_fills']} fills over {top['n_matches']} matches, "
              f"${top['notional']:,.0f}, {top['classification']}) "
              f"= {100*top['notional']/addressable_vol:.1f}% of addressable.")


if __name__ == "__main__":
    main()
