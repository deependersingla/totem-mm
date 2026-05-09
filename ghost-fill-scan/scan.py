"""
Polymarket ghost-fill / failed-settlement scanner.

Pulls recent txns to the CTF Exchange (V1) and NegRisk CTF Exchange (V2) from
Polygonscan, separates them by method selector and status, and reports:
  - how many matchOrders / fillOrder / fillOrders calls reverted
  - how many incrementNonce calls happened, and from which addresses
  - any reverted txns that mention the user's wallet in the calldata
"""

import os
import sys
import time
from collections import Counter, defaultdict
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_WALLET = "0x10b1E7827FCCeFEab27e751F4122DaB69d6adaA4".lower()

CTF_V1 = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
CTF_V2_NEGRISK = "0xc5d563a36ae78145c45a50134d48a1215220f80a"

# Method selectors (first 4 bytes of calldata)
SEL = {
    "0xfb0f3ee1": "fillOrder",
    "0x1c97d957": "fillOrders",
    "0xed03f2e3": "matchOrders",
    "0x627cdcb9": "incrementNonce",
    "0xb4e4ad57": "cancelOrder",
    "0x21275f5d": "cancelOrders",
    "0x16f0115b": "pause/admin",
}

API_KEY = os.environ.get("POLYGONSCAN_API_KEY", "")  # optional


def fetch(addr: str, page: int = 1, offset: int = 1000):
    params = {
        "module": "account",
        "action": "txlist",
        "address": addr,
        "startblock": 0,
        "endblock": 99999999,
        "page": page,
        "offset": offset,
        "sort": "desc",
    }
    if API_KEY:
        params["apikey"] = API_KEY
    url = "https://api.polygonscan.com/api?" + urlencode(params)
    req = Request(url, headers={"User-Agent": "ghost-fill-scan/1.0"})
    with urlopen(req, timeout=30) as r:
        import json
        return json.loads(r.read())


def selector(input_hex: str) -> str:
    if not input_hex or len(input_hex) < 10:
        return "0x"
    return input_hex[:10].lower()


def calldata_mentions(input_hex: str, addr_no_0x: str) -> bool:
    return addr_no_0x.lower() in (input_hex or "").lower()


def scan(name: str, addr: str):
    print(f"\n=== {name}  {addr} ===")
    try:
        resp = fetch(addr)
    except Exception as e:
        print(f"  fetch failed: {e}")
        return

    if resp.get("status") != "1":
        print(f"  api: {resp.get('message')} | {resp.get('result')!r}")
        return

    txs = resp["result"]
    if not txs:
        print("  no txns")
        return

    first_block = txs[-1]["blockNumber"]
    last_block = txs[0]["blockNumber"]
    first_ts = int(txs[-1]["timeStamp"])
    last_ts = int(txs[0]["timeStamp"])
    span_min = (last_ts - first_ts) / 60

    print(f"  pulled {len(txs)} txns | blocks {first_block}..{last_block} | span {span_min:.1f} min")

    by_method = Counter()
    by_method_failed = Counter()
    nonce_callers = Counter()
    failed_user_hits = []
    successful_user_hits = []

    user_no0x = USER_WALLET[2:]

    for tx in txs:
        sel = selector(tx.get("input", ""))
        method = SEL.get(sel, sel)
        is_err = tx.get("isError") == "1" or tx.get("txreceipt_status") == "0"
        by_method[method] += 1
        if is_err:
            by_method_failed[method] += 1
        if sel == "0x627cdcb9":
            nonce_callers[tx.get("from", "").lower()] += 1
        if calldata_mentions(tx.get("input", ""), user_no0x):
            entry = {
                "hash": tx["hash"],
                "method": method,
                "from": tx["from"],
                "block": tx["blockNumber"],
                "ts": tx["timeStamp"],
                "err": is_err,
            }
            if is_err:
                failed_user_hits.append(entry)
            else:
                successful_user_hits.append(entry)

    print("\n  Method breakdown (total / failed):")
    for m, n in by_method.most_common():
        f = by_method_failed[m]
        marker = "  <-- FAILED" if f else ""
        print(f"    {m:20s} {n:6d}  failed={f}{marker}")

    if nonce_callers:
        print(f"\n  incrementNonce callers (top 10 of {len(nonce_callers)}):")
        for who, n in nonce_callers.most_common(10):
            print(f"    {who}  x{n}")

    if successful_user_hits:
        print(f"\n  SUCCESSFUL txns mentioning user wallet ({len(successful_user_hits)}):")
        for e in successful_user_hits[:20]:
            print(f"    {e['hash']}  {e['method']:14s}  block {e['block']}  ts {e['ts']}")

    if failed_user_hits:
        print(f"\n  ** FAILED txns mentioning user wallet ({len(failed_user_hits)}) **")
        for e in failed_user_hits[:20]:
            print(f"    {e['hash']}  {e['method']:14s}  from {e['from']}  block {e['block']}")
    elif by_method_failed:
        print(f"\n  No reverted txns mention {USER_WALLET} in calldata.")
        print(f"  ({sum(by_method_failed.values())} total reverted txns to this contract in window)")


def main():
    print(f"Scanning Polymarket exchanges for failed settlements / nonce activity")
    print(f"Target user wallet: {USER_WALLET}")
    if not API_KEY:
        print("(no POLYGONSCAN_API_KEY set — using public rate limit, may be slow/limited)")
    scan("CTF V1", CTF_V1)
    time.sleep(0.3)
    scan("CTF V2 NegRisk", CTF_V2_NEGRISK)


if __name__ == "__main__":
    main()
