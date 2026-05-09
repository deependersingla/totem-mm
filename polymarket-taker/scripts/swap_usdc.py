"""
Swap native USDC → USDC.e on Polygon via QuickSwap V3 router.
1:1 peg, no slippage concern.

Usage:
    python swap_usdc.py <amount_usdc>    # e.g. python swap_usdc.py 50

Requires: pip install web3
"""

import sys
from web3 import Web3

# ── Config ────────────────────────────────────────────────────────────────────

# Your private key (same as in .env)
PRIVATE_KEY = ""  # Fill this or load from .env

RPC = "https://polygon-bor-rpc.publicnode.com"

USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

# QuickSwap V3 SwapRouter on Polygon
QUICKSWAP_ROUTER = Web3.to_checksum_address("0xf5b509bB0909a69B1c207E495f687a596C168E12")

# ERC-20 ABI (just approve + balanceOf)
ERC20_ABI = [
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

# QuickSwap V3 SwapRouter - exactInputSingle
SWAP_ABI = [
    {"inputs": [{"components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "limitSqrtPrice", "type": "uint160"},
    ], "name": "params", "type": "tuple"}],
     "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}], "type": "function"},
]


def main():
    if len(sys.argv) < 2:
        print("usage: python swap_usdc.py <amount_usdc>")
        sys.exit(1)

    amount_usdc = int(sys.argv[1])
    amount = amount_usdc * 10**6  # 6 decimals

    # Load key from .env if not hardcoded
    pk = PRIVATE_KEY
    if not pk:
        from pathlib import Path
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("POLYMARKET_PRIVATE_KEY="):
                    pk = line.split("=", 1)[1].strip()
                    break
    if not pk:
        print("set PRIVATE_KEY in script or POLYMARKET_PRIVATE_KEY in .env")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC))
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    account = w3.eth.account.from_key(pk)
    addr = account.address
    print(f"wallet: {addr}")

    usdc = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    usdc_e = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)

    bal = usdc.functions.balanceOf(addr).call()
    print(f"native USDC balance: {bal / 10**6}")
    if bal < amount:
        print(f"insufficient balance: have {bal / 10**6}, need {amount_usdc}")
        sys.exit(1)

    bal_e_before = usdc_e.functions.balanceOf(addr).call()
    print(f"USDC.e balance before: {bal_e_before / 10**6}")

    # Use pending nonce to handle any stuck txs
    nonce = w3.eth.get_transaction_count(addr, "pending")
    gas_price = w3.eth.gas_price
    # Add 50% buffer to current gas price for fast inclusion
    gas_price = int(gas_price * 1.5)
    print(f"using gas price: {gas_price / 10**9:.1f} gwei, nonce: {nonce}")

    # 1. Approve router to spend USDC
    print(f"approving {amount_usdc} USDC for router...")
    approve_tx = usdc.functions.approve(QUICKSWAP_ROUTER, amount).build_transaction({
        "from": addr,
        "nonce": nonce,
        "gas": 100_000,
        "gasPrice": gas_price,
    })
    signed = account.sign_transaction(approve_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"approve tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    print(f"approve confirmed (status={receipt['status']})")
    if receipt["status"] != 1:
        print("approve failed!")
        sys.exit(1)

    # 2. Swap USDC → USDC.e
    print(f"swapping {amount_usdc} USDC → USDC.e...")
    router = w3.eth.contract(address=QUICKSWAP_ROUTER, abi=SWAP_ABI)
    import time
    deadline = int(time.time()) + 300

    swap_params = (
        USDC_NATIVE,          # tokenIn
        USDC_E,               # tokenOut
        addr,                 # recipient
        deadline,             # deadline
        amount,               # amountIn
        amount * 99 // 100,   # amountOutMinimum (1% slippage, they're pegged)
        0,                    # limitSqrtPrice (0 = no limit)
    )

    # Refresh gas price for swap tx
    gas_price = int(w3.eth.gas_price * 1.2)
    swap_tx = router.functions.exactInputSingle(swap_params).build_transaction({
        "from": addr,
        "nonce": nonce + 1,
        "gas": 300_000,
        "gasPrice": gas_price,
    })
    signed = account.sign_transaction(swap_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"swap tx sent: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    print(f"swap tx: {tx_hash.hex()} (status={receipt['status']})")

    if receipt["status"] == 1:
        bal_e_after = usdc_e.functions.balanceOf(addr).call()
        gained = (bal_e_after - bal_e_before) / 10**6
        print(f"done! USDC.e balance: {bal_e_after / 10**6} (+{gained})")
    else:
        print("swap failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
