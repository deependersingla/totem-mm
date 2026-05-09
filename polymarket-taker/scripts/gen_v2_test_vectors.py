"""Generate V2 EIP-712 signing test vectors for the Rust taker.

Mirrors py-clob-client-v2's exchange_order_builder_v2.py exactly so the Rust
implementation can cross-check every intermediate (type hash, domain separator,
struct hash, EIP-712 digest, and final signature) against this fixture.

Run:
    venv/bin/python polymarket-taker/scripts/gen_v2_test_vectors.py

Output:
    polymarket-taker/tests/fixtures/v2_signing_vectors.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from eth_utils import keccak, to_checksum_address

# Pinned test private key — DO NOT use this for anything but tests.
# Address: 0xf39Fd6e51aad88F6F4ce6aB8827279cfffB92266 (well-known Hardhat #0)
TEST_PRIVKEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cfffB92266"

# V2 contract addresses (Polygon mainnet, chain 137) — from py-clob-client-v2/config.py
EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
CHAIN_ID = 137

# V2 type strings — verbatim from py-clob-client-v2 ctf_exchange_v2_typed_data.py
ORDER_TYPE_STRING = (
    b"Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    b"uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    b"uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
DOMAIN_TYPE_STRING = (
    b"EIP712Domain(string name,string version,uint256 chainId,"
    b"address verifyingContract)"
)
DOMAIN_NAME = b"Polymarket CTF Exchange"
DOMAIN_VERSION = b"2"

BYTES32_ZERO = "0x" + "00" * 32


def pad_uint256(v: int) -> bytes:
    return v.to_bytes(32, "big")


def pad_address(a: str) -> bytes:
    a = a.lower().replace("0x", "")
    assert len(a) == 40, a
    return bytes(12) + bytes.fromhex(a)


def pad_uint8(v: int) -> bytes:
    return bytes(31) + bytes([v])


def parse_bytes32(hex_str: str) -> bytes:
    s = hex_str.lower().replace("0x", "").zfill(64)
    assert len(s) == 64
    return bytes.fromhex(s)


@dataclass
class OrderV2:
    salt: int
    maker: str
    signer: str
    token_id: int
    maker_amount: int
    taker_amount: int
    side: int  # 0 = BUY, 1 = SELL
    signature_type: int  # 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE, 3=POLY_1271
    timestamp: int  # ms
    metadata: str  # 0x-prefixed 32-byte hex
    builder: str  # 0x-prefixed 32-byte hex (builderCode), zero if none
    expiration: int  # not in struct hash but in JSON body


def order_type_hash() -> bytes:
    return keccak(ORDER_TYPE_STRING)


def domain_separator(verifying_contract: str) -> bytes:
    return keccak(
        keccak(DOMAIN_TYPE_STRING)
        + keccak(DOMAIN_NAME)
        + keccak(DOMAIN_VERSION)
        + pad_uint256(CHAIN_ID)
        + pad_address(verifying_contract)
    )


def order_struct_hash(order: OrderV2) -> bytes:
    return keccak(
        order_type_hash()
        + pad_uint256(order.salt)
        + pad_address(order.maker)
        + pad_address(order.signer)
        + pad_uint256(order.token_id)
        + pad_uint256(order.maker_amount)
        + pad_uint256(order.taker_amount)
        + pad_uint8(order.side)
        + pad_uint8(order.signature_type)
        + pad_uint256(order.timestamp)
        + parse_bytes32(order.metadata)
        + parse_bytes32(order.builder)
    )


def eip712_digest(domain_sep: bytes, struct_hash: bytes) -> bytes:
    return keccak(b"\x19\x01" + domain_sep + struct_hash)


def sign_digest(privkey: str, digest: bytes) -> str:
    sig = Account._keys.PrivateKey(bytes.fromhex(privkey.replace("0x", ""))).sign_msg_hash(digest)
    # eth_keys returns r,s,v with v in {0,1}; serialize to 65 bytes r||s||v with v in {27,28}
    r = sig.r.to_bytes(32, "big")
    s = sig.s.to_bytes(32, "big")
    v = bytes([sig.v + 27]) if sig.v < 27 else bytes([sig.v])
    return "0x" + (r + s + v).hex()


def order_to_json(order: OrderV2, signature: str, owner: str) -> dict:
    side_str = "BUY" if order.side == 0 else "SELL"
    return {
        "order": {
            "salt": order.salt,
            "maker": to_checksum_address(order.maker),
            "signer": to_checksum_address(order.signer),
            "tokenId": str(order.token_id),
            "makerAmount": str(order.maker_amount),
            "takerAmount": str(order.taker_amount),
            "side": side_str,
            "expiration": str(order.expiration),
            "signatureType": order.signature_type,
            "timestamp": str(order.timestamp),
            "metadata": order.metadata,
            "builder": order.builder,
            "signature": signature,
        },
        "owner": owner,
        "orderType": "GTC",
        "deferExec": False,
        "postOnly": False,
    }


def make_vector(name: str, order: OrderV2, exchange_addr: str, neg_risk: bool, owner: str) -> dict:
    type_hash = order_type_hash()
    dom_sep = domain_separator(exchange_addr)
    struct_h = order_struct_hash(order)
    digest = eip712_digest(dom_sep, struct_h)
    signature = sign_digest(TEST_PRIVKEY, digest)
    return {
        "name": name,
        "input": {
            "privkey": TEST_PRIVKEY,
            "chain_id": CHAIN_ID,
            "exchange_address": exchange_addr,
            "neg_risk": neg_risk,
            "order": {
                "salt": str(order.salt),
                "maker": to_checksum_address(order.maker),
                "signer": to_checksum_address(order.signer),
                "token_id": str(order.token_id),
                "maker_amount": str(order.maker_amount),
                "taker_amount": str(order.taker_amount),
                "side": order.side,
                "signature_type": order.signature_type,
                "timestamp": str(order.timestamp),
                "metadata": order.metadata,
                "builder": order.builder,
                "expiration": str(order.expiration),
            },
        },
        "expected": {
            "type_hash": "0x" + type_hash.hex(),
            "domain_separator": "0x" + dom_sep.hex(),
            "struct_hash": "0x" + struct_h.hex(),
            "eip712_digest": "0x" + digest.hex(),
            "signature": signature,
            "json_body": order_to_json(order, signature, owner),
        },
    }


def main() -> None:
    # Pin every value so the fixture is deterministic. No time.time(), no random salts.
    proxy_wallet = "0x000000000000000000000000000000000000Beef"
    builder_code = "0x" + "11" * 32  # arbitrary non-zero builder code

    # Case 1: standard exchange, EOA signature, no builder code, no metadata.
    case1 = OrderV2(
        salt=12345678901234567890,
        maker=TEST_ADDR,
        signer=TEST_ADDR,
        token_id=int("71321045679252586228888434884664116321016356868447968942908055175859089714324"),
        maker_amount=10_000_000,  # 10 USDC
        taker_amount=20_000_000,  # at price 0.50
        side=0,  # BUY
        signature_type=0,  # EOA
        timestamp=1714435200000,
        metadata=BYTES32_ZERO,
        builder=BYTES32_ZERO,
        expiration=0,
    )

    # Case 2: standard exchange, POLY_PROXY signature, with builder code.
    # Salts are generated by py-clob-client-v2 as random.random() * time_ms,
    # so they're always well within u64 range (~10^12). Pin a realistic value.
    case2 = OrderV2(
        salt=987654321098765432,
        maker=proxy_wallet,
        signer=TEST_ADDR,
        token_id=int("71321045679252586228888434884664116321016356868447968942908055175859089714324"),
        maker_amount=5_000_000,
        taker_amount=12_500_000,  # price 0.40
        side=1,  # SELL
        signature_type=1,  # POLY_PROXY
        timestamp=1714435260000,
        metadata=BYTES32_ZERO,
        builder=builder_code,
        expiration=0,
    )

    # Case 3: neg-risk exchange, POLY_PROXY, with builder code.
    case3 = OrderV2(
        salt=42_000_000_000,
        maker=proxy_wallet,
        signer=TEST_ADDR,
        token_id=int("44444444444444444444444444444444444444444444444444444444444444444444444444444"),
        maker_amount=2_500_000,
        taker_amount=10_000_000,  # price 0.25
        side=0,  # BUY
        signature_type=1,  # POLY_PROXY
        timestamp=1714435320000,
        metadata=BYTES32_ZERO,
        builder=builder_code,
        expiration=0,
    )

    vectors = [
        make_vector("standard_eoa_no_builder", case1, EXCHANGE_V2, neg_risk=False, owner=TEST_ADDR),
        make_vector("standard_proxy_with_builder", case2, EXCHANGE_V2, neg_risk=False, owner=proxy_wallet),
        make_vector("negrisk_proxy_with_builder", case3, NEG_RISK_EXCHANGE_V2, neg_risk=True, owner=proxy_wallet),
    ]

    # Sanity: type hash and domain version are constants — assert before writing.
    assert vectors[0]["expected"]["type_hash"] == vectors[1]["expected"]["type_hash"], "type hash drift"
    assert vectors[0]["expected"]["type_hash"] == vectors[2]["expected"]["type_hash"], "type hash drift"

    out = {
        "_meta": {
            "description": "V2 EIP-712 signing test vectors. Source of truth for Rust orders_v2.rs.",
            "domain_name": DOMAIN_NAME.decode(),
            "domain_version": DOMAIN_VERSION.decode(),
            "order_type_string": ORDER_TYPE_STRING.decode(),
            "domain_type_string": DOMAIN_TYPE_STRING.decode(),
            "exchange_v2": EXCHANGE_V2,
            "neg_risk_exchange_v2": NEG_RISK_EXCHANGE_V2,
            "chain_id": CHAIN_ID,
        },
        "vectors": vectors,
    }

    out_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "v2_signing_vectors.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2) + "\n")
    print(f"wrote {len(vectors)} vectors → {out_path}")
    for v in vectors:
        print(f"  {v['name']:>32}  sig={v['expected']['signature'][:18]}...")


if __name__ == "__main__":
    main()
