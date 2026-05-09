"""Cross-check the hand-rolled V2 EIP-712 encoding against eth_account.encode_typed_data.

py-clob-client-v2 signs orders via:
    encoded = encode_typed_data(full_message=typed_data)
    signed = Account.sign_message(encoded, private_key=...)

This script reconstructs the same typed_data dict, runs it through eth_account,
and asserts the resulting signature matches what gen_v2_test_vectors.py produced.

If this passes, the fixture is faithful to the source-of-truth path.
"""

from __future__ import annotations

import json
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_typed_data


def main() -> None:
    fix_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v2_signing_vectors.json"
    fixtures = json.loads(fix_path.read_text())

    order_struct = [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
    ]
    domain_struct = [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ]

    for v in fixtures["vectors"]:
        i = v["input"]
        o = i["order"]
        privkey = i["privkey"]

        typed_data = {
            "primaryType": "Order",
            "types": {
                "EIP712Domain": domain_struct,
                "Order": order_struct,
            },
            "domain": {
                "name": "Polymarket CTF Exchange",
                "version": "2",
                "chainId": i["chain_id"],
                "verifyingContract": i["exchange_address"],
            },
            "message": {
                "salt": int(o["salt"]),
                "maker": o["maker"],
                "signer": o["signer"],
                "tokenId": int(o["token_id"]),
                "makerAmount": int(o["maker_amount"]),
                "takerAmount": int(o["taker_amount"]),
                "side": o["side"],
                "signatureType": o["signature_type"],
                "timestamp": int(o["timestamp"]),
                "metadata": bytes.fromhex(o["metadata"].replace("0x", "")),
                "builder": bytes.fromhex(o["builder"].replace("0x", "")),
            },
        }

        encoded = encode_typed_data(full_message=typed_data)
        signed = Account.sign_message(encoded, private_key=privkey)
        eth_account_sig = "0x" + signed.signature.hex()

        expected = v["expected"]["signature"]
        match = eth_account_sig.lower() == expected.lower()
        print(f"  {v['name']:>32}  {'OK' if match else 'MISMATCH'}")
        if not match:
            print(f"    fixture     : {expected}")
            print(f"    eth_account : {eth_account_sig}")
            raise SystemExit(1)

        # Also assert the digest derived by eth_account matches our hand-rolled one
        # encoded.body is the struct_hash, encoded.header is the domain separator.
        ea_digest = ("0x" + bytes(b"\x19\x01" + encoded.header + encoded.body).hex())
        # We can't directly compare to fixture digest without rehashing — recompute:
        from eth_utils import keccak
        ea_digest_hash = "0x" + keccak(b"\x19\x01" + encoded.header + encoded.body).hex()
        if ea_digest_hash.lower() != v["expected"]["eip712_digest"].lower():
            print(f"    digest mismatch: fixture={v['expected']['eip712_digest']} eth_account={ea_digest_hash}")
            raise SystemExit(1)

    print(f"all {len(fixtures['vectors'])} vectors agree with eth_account.encode_typed_data")


if __name__ == "__main__":
    main()
