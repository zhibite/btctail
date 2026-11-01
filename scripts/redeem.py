#!/usr/bin/env python
"""Optional: redeem winning conditional-token positions on-chain.

WHY THIS IS OPTIONAL
--------------------
Polymarket does not push the resolution payout into your wallet. Winning
outcome tokens stay as ERC-1155 balances until someone calls the CTF's
``redeemPositions``. The dashboard tracks the *economic* PnL and flags
unredeemed winners; you can always redeem them in the Polymarket UI
(https://polymarket.com/portfolio). This script is for operators who would
rather automate the claim.

SAFETY
------
* Dry run by default: prints the transaction, sends nothing.
* Only handles **standard binary CTF markets**. Neg-risk markets are
  skipped with an explanation (redeem those in the UI).
* Only works when the signing key is the wallet that holds the tokens
  (an EOA). Proxy / Safe wallets (SIGNATURE_TYPE 1/2) must redeem through
  the Polymarket UI or a relayed transaction -- this script refuses.
* Sending a transaction costs MATIC and is irreversible. Review the dry run.

USAGE
-----
    pip install web3
    python scripts/redeem.py                 # dry run
    python scripts/redeem.py --execute       # send
    python scripts/redeem.py --trade 12 --execute
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings as load_settings  # noqa: E402
from src.persistence.db import DB  # noqa: E402

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PARENT_COLLECTION_ID = "0x" + "00" * 32
POLYGON_CHAIN_ID = 137

REDEEM_ABI = json.loads(
    '[{"inputs":['
    '{"internalType":"address","name":"collateralToken"},'
    '{"internalType":"bytes32","name":"parentCollectionId"},'
    '{"internalType":"bytes32","name":"conditionId"},'
    '{"internalType":"uint256[]","name":"indexSets"}],'
    '"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable",'
    '"type":"function"}]'
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Redeem resolved Polymarket positions")
    parser.add_argument("--execute", action="store_true", help="actually send transactions")
    parser.add_argument("--trade", type=int, default=0, help="only this trade id")
    parser.add_argument("--rpc", default="https://polygon-rpc.com")
    args = parser.parse_args()

    cfg = load_settings()
    db = DB(cfg.db_path)
    rows = db.unredeemed_trades()
    if args.trade:
        rows = [row for row in rows if int(row["id"]) == args.trade]
    if not rows:
        print("没有待赎回的获胜仓位。")
        return 0

    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError:
        print("需要 web3：pip install web3", file=sys.stderr)
        return 2

    if not cfg.WALLET_PRIVATE_KEY:
        print("需要 WALLET_PRIVATE_KEY。", file=sys.stderr)
        return 2

    account = Account.from_key(cfg.WALLET_PRIVATE_KEY)
    if cfg.FUNDER_ADDRESS and cfg.FUNDER_ADDRESS.lower() != account.address.lower():
        print(
            "签名地址与 FUNDER_ADDRESS 不同：代币在代理/Safe 钱包里，直接签名无法赎回。\n"
            "请在 Polymarket 持仓页手动赎回，或使用转发的 Safe 交易。",
            file=sys.stderr,
        )
        return 2

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 20}))
    if not w3.is_connected():
        print(f"无法连接 RPC: {args.rpc}", file=sys.stderr)
        return 2

    contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=REDEEM_ABI)
    nonce = w3.eth.get_transaction_count(account.address)
    sent = 0
    for row in rows:
        condition_id = row.get("condition_id") or ""
        slug = ""
        window = db.get_window(int(row["slot"]))
        if window:
            slug = window.get("slug") or ""
        if not condition_id.startswith("0x") or len(condition_id) != 66:
            print(f"[skip] trade {row['id']}: 无效 condition_id {condition_id!r}")
            continue
        try:
            tx = contract.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                bytes.fromhex(PARENT_COLLECTION_ID[2:]),
                bytes.fromhex(condition_id[2:]),
                [1, 2],
            ).build_transaction({
                "from": account.address,
                "nonce": nonce,
                "chainId": POLYGON_CHAIN_ID,
                "gas": 250_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            })
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] trade {row['id']}: 构建交易失败: {exc}")
            continue

        print(
            f"[{'send' if args.execute else 'dry-run'}] trade {row['id']} "
            f"slot={row['slot']} {slug} condition={condition_id[:12]}…"
        )
        if not args.execute:
            print(f"         to={tx['to']} data={tx['data'][:66]}… gas={tx['gas']}")
            continue
        try:
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            ok = receipt.status == 1
            print(f"         tx={tx_hash.hex()} status={'ok' if ok else 'reverted'}")
            if ok:
                db.set_redeem_status(int(row["id"]), "done")
                sent += 1
            nonce += 1
        except Exception as exc:  # noqa: BLE001
            print(f"         失败: {exc}")

    if args.execute:
        print(f"完成：{sent}/{len(rows)} 笔已赎回。")
    else:
        print("这是 dry-run。确认无误后加 --execute。")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
