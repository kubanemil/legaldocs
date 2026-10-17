"""Anchoring receipt hashes on the ReceiptRegistry contract.

Workers never deploy. They talk to the one registry at REGISTRY_ADDRESS, and the deploy
step (`python -m parent.chain deploy`) refuses to put it anywhere else.
"""

import argparse
import json
import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from web3 import Web3
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.types import TxParams

ARTIFACT: Final = Path(__file__).resolve().parent.parent / "contracts" / "ReceiptRegistry.json"
RECEIPT_TIMEOUT_SECONDS: Final = 60

log = logging.getLogger("parent.chain")


class ChainError(RuntimeError):
    pass


@dataclass(frozen=True)
class Anchored:
    tx_hash: str
    timestamp: int


def load_artifact() -> dict[str, Any]:
    artifact: dict[str, Any] = json.loads(ARTIFACT.read_text())
    return artifact


class Relayer:
    def __init__(
        self,
        w3: Web3,
        registry_address: str,
        private_key: str,
        nonce_lock: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        address = Web3.to_checksum_address(registry_address)
        if not w3.eth.get_code(address):
            raise ChainError(f"no ReceiptRegistry at {address}; run the deploy step first")
        self._w3 = w3
        self._account: LocalAccount = Account.from_key(private_key)
        self._contract = w3.eth.contract(address=address, abi=load_artifact()["abi"])
        self._nonce_lock = nonce_lock

    def anchored_at(self, receipt_hash: bytes) -> int:
        timestamp: int = self._contract.functions.anchoredAt(receipt_hash).call()
        return timestamp

    def anchor(
        self, receipt_hash: bytes, *, known_tx: str | None, on_sent: Callable[[str], None]
    ) -> Anchored:
        if known_tx is not None:
            self._settle(known_tx)
        if timestamp := self.anchored_at(receipt_hash):
            return Anchored(known_tx or self._find_tx(receipt_hash), timestamp)

        with self._nonce_lock():
            nonce = self._w3.eth.get_transaction_count(self._account.address, "pending")
            params: TxParams = {"from": self._account.address, "nonce": nonce}
            tx = self._contract.functions.anchor(receipt_hash).build_transaction(params)
            signed = self._account.sign_transaction(cast(dict[str, Any], tx))
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction).to_0x_hex()
        on_sent(tx_hash)

        mined = self._w3.eth.wait_for_transaction_receipt(
            HexStr(tx_hash), timeout=RECEIPT_TIMEOUT_SECONDS
        )
        if mined["status"] != 1:
            raise ChainError(f"anchor transaction {tx_hash} reverted")
        return Anchored(tx_hash, self.anchored_at(receipt_hash))

    def _settle(self, tx_hash: str) -> None:
        try:
            self._w3.eth.wait_for_transaction_receipt(
                HexStr(tx_hash), timeout=RECEIPT_TIMEOUT_SECONDS
            )
        except (TimeExhausted, TransactionNotFound):
            log.warning("earlier anchor tx %s not mined; sending a new one", tx_hash)

    def _find_tx(self, receipt_hash: bytes) -> str:
        events = self._contract.events.ReceiptAnchored.get_logs(
            argument_filters={"receiptHash": receipt_hash}, from_block=0
        )
        return events[0]["transactionHash"].to_0x_hex() if events else ""


def deploy(w3: Web3, owner_key: str, relayer_address: str) -> str:
    owner: LocalAccount = Account.from_key(owner_key)
    artifact = load_artifact()
    factory = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])
    params: TxParams = {
        "from": owner.address,
        "nonce": w3.eth.get_transaction_count(owner.address, "pending"),
    }
    tx = factory.constructor(Web3.to_checksum_address(relayer_address)).build_transaction(params)
    tx_hash = w3.eth.send_raw_transaction(
        owner.sign_transaction(cast(dict[str, Any], tx)).raw_transaction
    )
    mined = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT_SECONDS)
    address = mined["contractAddress"]
    if mined["status"] != 1 or address is None:
        raise ChainError("registry deployment failed")
    return str(address)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy the ReceiptRegistry once.")
    parser.add_argument("command", choices=["deploy"])
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    w3 = Web3(Web3.HTTPProvider(os.environ["ETH_RPC_URL"]))
    expected = Web3.to_checksum_address(os.environ["REGISTRY_ADDRESS"])
    if w3.eth.get_code(expected):
        log.info("registry already deployed at %s", expected)
        return
    address = deploy(w3, os.environ["OWNER_KEY"], os.environ["RELAYER_ADDRESS"])
    if address != expected:
        raise SystemExit(f"registry deployed at {address}, but REGISTRY_ADDRESS is {expected}")
    log.info("registry deployed at %s", address)


if __name__ == "__main__":
    main()
