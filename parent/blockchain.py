"""Ethereum blockchain integration for document hash anchoring.

Deploys (or connects to) the DocumentRegistry contract and provides
helpers to anchor document hashes on-chain and verify them later.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from web3 import Web3
from web3.contract import Contract

logger = logging.getLogger("blockchain")

_w3: Optional[Web3] = None
_contract: Optional[Contract] = None
_account: Optional[str] = None

CONTRACT_ARTIFACT = Path(__file__).resolve().parent.parent / "contracts" / "DocumentRegistry.json"


def _load_artifact() -> dict:
    with open(CONTRACT_ARTIFACT) as f:
        return json.load(f)


def get_web3() -> Web3:
    global _w3
    if _w3 is None:
        rpc_url = os.getenv("ETH_RPC_URL", "http://ganache:8545")
        _w3 = Web3(Web3.HTTPProvider(rpc_url))
    return _w3


def get_account() -> str:
    global _account
    if _account is None:
        w3 = get_web3()
        private_key = os.getenv("ETH_PRIVATE_KEY", "")
        if private_key:
            acct = w3.eth.account.from_key(private_key)
            _account = acct.address
        else:
            _account = w3.eth.accounts[0]
    return _account


def get_contract() -> Contract:
    global _contract
    if _contract is not None:
        return _contract

    w3 = get_web3()
    artifact = _load_artifact()
    contract_address = os.getenv("REGISTRY_CONTRACT_ADDRESS", "")

    if contract_address:
        _contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address),
            abi=artifact["abi"],
        )
    else:
        _contract = _deploy_contract(w3, artifact)

    return _contract


def _deploy_contract(w3: Web3, artifact: dict) -> Contract:
    """Deploy DocumentRegistry and return the contract instance."""
    account = get_account()
    contract_class = w3.eth.contract(abi=artifact["abi"], bytecode=artifact["bytecode"])

    tx_hash = contract_class.constructor().transact({"from": account})
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)

    logger.info(
        "DocumentRegistry deployed",
        extra={"address": receipt.contractAddress, "tx_hash": receipt.transactionHash.hex()},
    )

    return w3.eth.contract(address=receipt.contractAddress, abi=artifact["abi"])


def compute_hash(data: bytes) -> bytes:
    """Compute keccak-256 hash of data, returned as 32 bytes."""
    return Web3.keccak(data)


def anchor_document(document_id: str, content_hash: bytes, result_hash: bytes, tenant_id: str) -> str:
    """Anchor document hashes on-chain. Returns the transaction hash hex string."""
    w3 = get_web3()
    contract = get_contract()
    account = get_account()

    private_key = os.getenv("ETH_PRIVATE_KEY", "")
    if private_key:
        tx = contract.functions.anchorDocument(
            document_id, content_hash, result_hash, tenant_id
        ).build_transaction({
            "from": account,
            "nonce": w3.eth.get_transaction_count(account),
            "gas": 300_000,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    else:
        tx_hash = contract.functions.anchorDocument(
            document_id, content_hash, result_hash, tenant_id
        ).transact({"from": account})

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    return receipt.transactionHash.hex()


def verify_document(document_id: str) -> dict | None:
    """Look up an anchored document. Returns None if not found."""
    contract = get_contract()
    if not contract.functions.isAnchored(document_id).call():
        return None

    content_hash, result_hash, tenant_id, timestamp = contract.functions.getAnchor(document_id).call()
    return {
        "content_hash": "0x" + content_hash.hex(),
        "result_hash": "0x" + result_hash.hex(),
        "tenant_id": tenant_id,
        "timestamp": timestamp,
    }
