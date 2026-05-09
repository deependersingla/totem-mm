"""
Polymarket ghost-fill scanner via direct Polygon JSON-RPC.

Scans the last N blocks for txns to the CTF Exchange (V1) and NegRisk (V2),
checks receipt status, and reports:
  - failed matchOrders / fillOrder / fillOrders (= ghost fills)
  - incrementNonce calls and who is making them
  - any txn (failed or successful) whose calldata mentions USER_WALLET
"""

import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request, urlopen

USER_WALLET = "0x10b1E7827FCCeFEab27e751F4122DaB69d6adaA4".lower()
USER_NO0X = USER_WALLET[2:]

CTF_V1 = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
CTF_V1_NEGRISK = "0xc5d563a36ae78145c45a50134d48a1215220f80a"
CTF_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
CTF_V2_NEGRISK = "0xe2222d279d744050d28e00520010520000310f59"
TARGETS = {CTF_V1, CTF_V1_NEGRISK, CTF_V2, CTF_V2_NEGRISK}
LABEL = {
    CTF_V1: "V1",
    CTF_V1_NEGRISK: "V1-NegRisk",
    CTF_V2: "V2",
    CTF_V2_NEGRISK: "V2-NegRisk",
}

SEL = {
    "0xfb0f3ee1": "fillOrder",
    "0x1c97d957": "fillOrders",
    "0xed03f2e3": "matchOrders",
    "0x627cdcb9": "incrementNonce",
    "0xb4e4ad57": "cancelOrder",
    "0x21275f5d": "cancelOrders",
}

RPC = os.environ.get("POLYGON_RPC", "https://polygon-rpc.com")
BLOCKS = int(os.environ.get("BLOCKS", "500"))  # ~16 min
WORKERS = int(os.environ.get("WORKERS", "12"))


HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 ghost-fill-scan/1.0"}


def rpc(method: str, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = Request(RPC, data=body, headers=HDRS)
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())["result"]


def rpc_batch(calls):
    body = json.dumps([
        {"jsonrpc": "2.0", "id": i, "method": m, "params": p}
        for i, (m, p) in enumerate(calls)
    ]).encode()
    req = Request(RPC, data=body, headers=HDRS)
    with urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if not isinstance(out, list):
        # Some RPCs reject batch and return single error obj
        raise RuntimeError(f"non-batch response: {out}")
    out.sort(key=lambda x: x["id"])
    return [x.get("result") for x in out]


def selector(input_hex):
    if not input_hex or len(input_hex) < 10:
        return "0x"
    return input_hex[:10].lower()


def fetch_block(n_hex):
    return rpc("eth_getBlockByNumber", [n_hex, True])


def main():
    print(f"RPC: {RPC}")
    print(f"User wallet: {USER_WALLET}")
    head_hex = rpc("eth_blockNumber", [])
    head = int(head_hex, 16)
    start = head - BLOCKS + 1
    print(f"Scanning blocks {start}..{head}  ({BLOCKS} blocks, ~{BLOCKS*2/60:.1f} min of chain time)")

    t0 = time.time()
    blocks = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch_block, hex(n)): n for n in range(start, head + 1)}
        for fut in as_completed(futs):
            try:
                blocks.append(fut.result())
            except Exception as e:
                print(f"  block fetch err for {futs[fut]}: {e}", file=sys.stderr)
    print(f"fetched {len(blocks)} blocks in {time.time()-t0:.1f}s")

    # Collect target txns
    target_txns = []  # list of dicts with hash, to, from, input, blockNumber
    for b in blocks:
        if not b:
            continue
        for tx in b.get("transactions", []):
            to = (tx.get("to") or "").lower()
            if to in TARGETS:
                target_txns.append({
                    "hash": tx["hash"],
                    "to": to,
                    "from": (tx.get("from") or "").lower(),
                    "input": tx.get("input", ""),
                    "block": int(tx["blockNumber"], 16),
                    "ts": int(b["timestamp"], 16),
                })
    print(f"found {len(target_txns)} txns to CTF exchanges")

    # Batch fetch receipts
    receipts = {}
    t1 = time.time()
    BATCH = 50
    hashes = [tx["hash"] for tx in target_txns]
    for i in range(0, len(hashes), BATCH):
        chunk = hashes[i:i+BATCH]
        results = rpc_batch([("eth_getTransactionReceipt", [h]) for h in chunk])
        for h, r in zip(chunk, results):
            receipts[h] = r
    print(f"fetched {len(receipts)} receipts in {time.time()-t1:.1f}s")

    # Analyze per exchange
    for addr in (CTF_V1, CTF_V1_NEGRISK, CTF_V2, CTF_V2_NEGRISK):
        txs = [t for t in target_txns if t["to"] == addr]
        if not txs:
            print(f"\n=== {LABEL[addr]}  {addr} ===  no txns")
            continue
        print(f"\n=== {LABEL[addr]}  {addr} ===  ({len(txs)} txns)")
        by_method = Counter()
        failed_method = Counter()
        nonce_callers = Counter()
        user_hits_failed = []
        user_hits_ok = []

        for tx in txs:
            sel = selector(tx["input"])
            m = SEL.get(sel, sel)
            r = receipts.get(tx["hash"]) or {}
            status = r.get("status")
            failed = status == "0x0"
            by_method[m] += 1
            if failed:
                failed_method[m] += 1
            if sel == "0x627cdcb9":
                nonce_callers[tx["from"]] += 1
            if USER_NO0X in tx["input"].lower():
                e = {**tx, "method": m, "failed": failed}
                (user_hits_failed if failed else user_hits_ok).append(e)

        print("  method                total   failed")
        for m, n in by_method.most_common():
            f = failed_method[m]
            mark = "  <-- ghost-fill candidate" if f and m in ("matchOrders", "fillOrder", "fillOrders") else ""
            print(f"  {m:20s} {n:6d}   {f:4d}{mark}")

        total_failed = sum(failed_method.values())
        total = sum(by_method.values())
        if total:
            print(f"  failure rate: {total_failed}/{total} = {100*total_failed/total:.2f}%")

        if nonce_callers:
            print(f"\n  incrementNonce callers in window: {len(nonce_callers)} unique addrs, {sum(nonce_callers.values())} calls")
            for who, n in nonce_callers.most_common(15):
                print(f"    {who}  x{n}")

        if user_hits_ok:
            print(f"\n  Successful txns mentioning {USER_WALLET}: {len(user_hits_ok)}")
            for e in user_hits_ok[:10]:
                print(f"    {e['hash']}  {e['method']:14s}  blk {e['block']}")
        if user_hits_failed:
            print(f"\n  ** FAILED txns mentioning {USER_WALLET}: {len(user_hits_failed)} ** ")
            for e in user_hits_failed[:10]:
                print(f"    {e['hash']}  {e['method']:14s}  from {e['from']}  blk {e['block']}")
        elif user_hits_ok == [] and (total_failed or by_method):
            print(f"\n  No txns in this {BLOCKS}-block window reference {USER_WALLET}.")


if __name__ == "__main__":
    main()
