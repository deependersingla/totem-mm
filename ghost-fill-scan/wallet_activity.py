"""
Direct wallet-activity check.

For the user's safe wallet, queries the Polygon node for ALL ERC-20 Transfer
events and ERC-1155 TransferSingle/Batch events where the wallet is sender
or receiver, in the last N blocks. This is the source of truth for whether
*any* trade actually settled to the wallet.
"""

import json
import os
import sys
import time
from collections import Counter
from urllib.request import Request, urlopen

USER = "0x10b1E7827FCCeFEab27e751F4122DaB69d6adaA4".lower()
USER_TOPIC = "0x" + "0" * 24 + USER[2:]

ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ERC1155_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ERC1155_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

# Known token addresses on Polygon
USDC_E = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"  # legacy bridged USDC
USDC_NATIVE = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"  # native USDC
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"  # Polymarket USD proxy
CTF_TOKENS = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"  # Conditional Tokens (ERC-1155)

TOKEN_LABEL = {
    USDC_E.lower(): "USDC.e",
    USDC_NATIVE.lower(): "USDC",
    PUSD.lower(): "pUSD",
    CTF_TOKENS.lower(): "CTF (ERC-1155)",
}

RPC = os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
HOURS = float(os.environ.get("HOURS", "6"))
HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 wallet-activity/1.0"}


def rpc(method, params, retries=3):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for i in range(retries):
        try:
            req = Request(RPC, data=body, headers=HDRS)
            with urlopen(req, timeout=60) as r:
                resp = json.loads(r.read())
            if "error" in resp:
                raise RuntimeError(resp["error"])
            return resp["result"]
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err


def get_logs_chunked(from_block, to_block, topics, addresses=None, chunk=1000):
    """Get logs in chunks. Public RPCs require an address filter."""
    out = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk - 1, to_block)
        flt = {
            "fromBlock": hex(cur),
            "toBlock": hex(end),
            "topics": topics,
        }
        if addresses:
            flt["address"] = addresses if len(addresses) > 1 else addresses[0]
        try:
            logs = rpc("eth_getLogs", [flt])
            out.extend(logs)
        except Exception as e:
            print(f"  getLogs {cur}..{end}: {e}", file=sys.stderr)
        cur = end + 1
    return out


def main():
    head = int(rpc("eth_blockNumber", []), 16)
    blocks_back = int(HOURS * 3600 / 2)  # Polygon ~2s per block
    start = head - blocks_back
    print(f"User: {USER}")
    print(f"Window: blocks {start}..{head}  ({blocks_back} blocks, ~{HOURS:.1f}h)")
    print(f"RPC: {RPC}")

    erc20_addrs = [USDC_E, USDC_NATIVE, PUSD]
    erc1155_addrs = [CTF_TOKENS]

    queries = [
        ("ERC-20 OUT (user→x)", [ERC20_TRANSFER, USER_TOPIC, None], erc20_addrs),
        ("ERC-20 IN  (x→user)", [ERC20_TRANSFER, None, USER_TOPIC], erc20_addrs),
        ("ERC1155-1 OUT (user→x)", [ERC1155_SINGLE, None, USER_TOPIC, None], erc1155_addrs),
        ("ERC1155-1 IN  (x→user)", [ERC1155_SINGLE, None, None, USER_TOPIC], erc1155_addrs),
        ("ERC1155-B OUT (user→x)", [ERC1155_BATCH, None, USER_TOPIC, None], erc1155_addrs),
        ("ERC1155-B IN  (x→user)", [ERC1155_BATCH, None, None, USER_TOPIC], erc1155_addrs),
    ]

    grand = []
    for name, topics, addrs in queries:
        t0 = time.time()
        logs = get_logs_chunked(start, head, topics, addresses=addrs, chunk=2000)
        print(f"  {name:24s}  {len(logs):4d} events  ({time.time()-t0:.1f}s)")
        for lg in logs:
            grand.append((name, lg))

    if not grand:
        print("\n>>> ZERO transfers in/out of this wallet in the entire window. <<<")
        print("    → No trade has settled to/from this safe in the last", HOURS, "hours.")
        print("    → Confirms ghost-fill (or the wallet is genuinely idle).")
        return

    # Aggregate by token contract
    by_token = Counter()
    by_token_dir = Counter()  # (token, direction)
    for name, lg in grand:
        addr = lg["address"].lower()
        by_token[addr] += 1
        direction = "OUT" if "OUT" in name else "IN"
        by_token_dir[(addr, direction)] += 1

    print(f"\nTotal transfer events touching wallet: {len(grand)}")
    print("\nBy token contract:")
    for tok, n in by_token.most_common():
        label = TOKEN_LABEL.get(tok, tok)
        ins = by_token_dir.get((tok, "IN"), 0)
        outs = by_token_dir.get((tok, "OUT"), 0)
        print(f"  {label:18s} {tok}  total={n:4d}  in={ins} out={outs}")

    # Show last 15 events with block, tx, direction
    print("\nMost recent 15 events:")
    grand.sort(key=lambda x: int(x[1]["blockNumber"], 16), reverse=True)
    for name, lg in grand[:15]:
        addr = lg["address"].lower()
        label = TOKEN_LABEL.get(addr, addr[:10] + "...")
        blk = int(lg["blockNumber"], 16)
        tx = lg["transactionHash"]
        print(f"  blk {blk}  {label:18s}  {name:24s}  {tx}")


if __name__ == "__main__":
    main()
