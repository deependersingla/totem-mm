"""
Check the user's safe wallet's approvals against the V2 exchange contracts.

If pUSD allowance is 0 for the V2 exchanges, OR isApprovedForAll on the
Conditional Tokens is false for the V2 exchanges, then settlement would
revert at the allowance check — explaining ghost fills.
"""

import json
import os
from urllib.request import Request, urlopen

USER = "0x10b1E7827FCCeFEab27e751F4122DaB69d6adaA4".lower()

PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
USDC_E = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
USDC_NATIVE = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
CTF_TOKENS = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"

CTF_V1 = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
CTF_V1_NEGRISK = "0xc5d563a36ae78145c45a50134d48a1215220f80a"
CTF_V2 = "0xe111180000d2663c0091e4f400237545b87b996b"
CTF_V2_NEGRISK = "0xe2222d279d744050d28e00520010520000310f59"

LABEL = {
    CTF_V1: "V1 CTFExchange",
    CTF_V1_NEGRISK: "V1 NegRisk",
    CTF_V2: "V2 CTFExchange",
    CTF_V2_NEGRISK: "V2 NegRisk",
}
TOK_LABEL = {
    PUSD: "pUSD",
    USDC_E: "USDC.e",
    USDC_NATIVE: "USDC",
    CTF_TOKENS: "CTF (1155)",
}

RPC = os.environ.get("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
HDRS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 approvals/1.0"}


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = Request(RPC, data=body, headers=HDRS)
    with urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
    if "error" in resp:
        raise RuntimeError(resp["error"])
    return resp["result"]


def pad(addr):
    return "0" * 24 + addr.lower().lstrip("0x")


def call(to, data):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def allowance(token, owner, spender):
    # allowance(address,address) selector = 0xdd62ed3e
    data = "0xdd62ed3e" + pad(owner) + pad(spender)
    res = call(token, data)
    return int(res, 16)


def is_approved_for_all(token, owner, operator):
    # isApprovedForAll(address,address) selector = 0xe985e9c5
    data = "0xe985e9c5" + pad(owner) + pad(operator)
    res = call(token, data)
    return int(res, 16) != 0


def balance_of(token, owner):
    # balanceOf(address) selector = 0x70a08231
    data = "0x70a08231" + pad(owner)
    res = call(token, data)
    return int(res, 16)


def main():
    print(f"User wallet: {USER}\n")

    print("== Token balances ==")
    for tok in (USDC_E, USDC_NATIVE, PUSD):
        try:
            bal = balance_of(tok, USER)
            print(f"  {TOK_LABEL[tok]:8s}  {bal/1e6:>14,.2f}    ({tok})")
        except Exception as e:
            print(f"  {TOK_LABEL[tok]:8s}  err: {e}")

    print("\n== ERC-20 allowances (token -> exchange) ==")
    for tok in (PUSD, USDC_E, USDC_NATIVE):
        for ex in (CTF_V1, CTF_V1_NEGRISK, CTF_V2, CTF_V2_NEGRISK):
            try:
                a = allowance(tok, USER, ex)
                if a > 10**70:
                    s = "MAX"
                else:
                    s = f"{a/1e6:,.2f}"
                tag = "  <-- needed for V2" if (tok == PUSD and ex in (CTF_V2, CTF_V2_NEGRISK) and a == 0) else ""
                print(f"  {TOK_LABEL[tok]:8s} -> {LABEL[ex]:18s}  allowance = {s}{tag}")
            except Exception as e:
                print(f"  {TOK_LABEL[tok]:8s} -> {LABEL[ex]:18s}  err: {e}")

    print("\n== ERC-1155 isApprovedForAll (CTF tokens -> exchange) ==")
    for ex in (CTF_V1, CTF_V1_NEGRISK, CTF_V2, CTF_V2_NEGRISK):
        try:
            ok = is_approved_for_all(CTF_TOKENS, USER, ex)
            tag = "  <-- needed for V2 sells" if (ex in (CTF_V2, CTF_V2_NEGRISK) and not ok) else ""
            print(f"  CTF -> {LABEL[ex]:18s}  approved = {ok}{tag}")
        except Exception as e:
            print(f"  CTF -> {LABEL[ex]:18s}  err: {e}")


if __name__ == "__main__":
    main()
