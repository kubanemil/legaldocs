import os
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from eth_account import Account
from eth_typing import HexStr
from web3 import Web3
from web3.exceptions import ContractLogicError

from client.sdk import anchored_at
from parent.chain import Anchored, ChainError, Deployment, Relayer, deploy

ROOT = Path(__file__).resolve().parent.parent
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
RELAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
REGISTRY = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
STRANGER_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"


def run_anvil(*flags: str) -> Iterator[str]:
    binary = shutil.which("anvil")
    if binary is None:
        if os.environ.get("CI"):
            pytest.fail("anvil is required in CI")
        pytest.skip("anvil (Foundry) is not installed")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([binary, "--port", str(port), "--silent", *flags])
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while not Web3(Web3.HTTPProvider(url)).is_connected():
        if time.monotonic() > deadline:
            process.kill()
            pytest.fail("anvil did not start")
        time.sleep(0.1)
    yield url
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture
def rpc_url() -> Iterator[str]:
    yield from run_anvil()


@pytest.fixture
def slow_rpc_url() -> Iterator[str]:
    yield from run_anvil("--block-time", "1")


def relayer_for(
    url: str, key: str = RELAYER_KEY, lock: threading.Lock | None = None, **options: int
) -> tuple[Relayer, Deployment]:
    w3 = Web3(Web3.HTTPProvider(url))
    deployment = deploy(w3, OWNER_KEY, Account.from_key(RELAYER_KEY).address, REGISTRY)
    shared = lock or threading.Lock()
    options.setdefault("deploy_block", deployment.block)
    relayer = Relayer(w3, deployment.address, key, lambda: shared, **options)
    return relayer, deployment


def nonce(url: str, key: str = RELAYER_KEY) -> int:
    return Web3(Web3.HTTPProvider(url)).eth.get_transaction_count(
        Account.from_key(key).address, "pending"
    )


def test_the_registry_lands_where_compose_expects_it(rpc_url: str) -> None:
    _, deployment = relayer_for(rpc_url)
    compose = (ROOT / "docker-compose.yml").read_text()
    assert f'REGISTRY_ADDRESS: "{deployment.address}"' in compose
    assert f'REGISTRY_DEPLOY_BLOCK: "{deployment.block}"' in compose


def test_deploy_refuses_to_send_a_deployment_that_would_land_elsewhere(rpc_url: str) -> None:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    owner = Account.from_key(OWNER_KEY)
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(
            owner.sign_transaction(
                {
                    "to": owner.address,
                    "value": 0,
                    "gas": 21_000,
                    "gasPrice": 10**10,
                    "nonce": 0,
                    "chainId": w3.eth.chain_id,
                }
            ).raw_transaction
        )
    )
    with pytest.raises(ChainError, match="would land"):
        deploy(w3, OWNER_KEY, Account.from_key(RELAYER_KEY).address, REGISTRY)
    assert nonce(rpc_url, OWNER_KEY) == 1
    assert not w3.eth.get_code(Web3.to_checksum_address(REGISTRY))


def test_anchoring_is_idempotent(rpc_url: str) -> None:
    relayer, _ = relayer_for(rpc_url)
    sent: list[str] = []
    first = relayer.anchor(b"\x01" * 32, known_tx=None, on_sent=sent.append)
    assert sent == [first.tx_hash]
    assert first.timestamp == relayer.anchored_at(b"\x01" * 32) > 0

    used = nonce(rpc_url)
    again = relayer.anchor(b"\x01" * 32, known_tx=first.tx_hash, on_sent=sent.append)
    assert again == first
    assert nonce(rpc_url) == used


def lose_a_transaction(relayer: Relayer, rpc_url: str, receipt_hash: bytes) -> str:
    lost: list[str] = []

    def crash(tx_hash: str) -> None:
        lost.append(tx_hash)
        raise RuntimeError("worker died before saving the tx hash")

    with pytest.raises(RuntimeError):
        relayer.anchor(receipt_hash, known_tx=None, on_sent=crash)
    Web3(Web3.HTTPProvider(rpc_url)).eth.wait_for_transaction_receipt(HexStr(lost[0]))
    return lost[0]


def test_a_relayer_that_lost_its_tx_hash_finds_it_by_scanning_back_in_windows(
    rpc_url: str,
) -> None:
    relayer, _ = relayer_for(rpc_url, log_window=1)
    lost = lose_a_transaction(relayer, rpc_url, b"\x02" * 32)
    for filler in range(3):
        relayer.anchor(bytes([filler + 10]) * 32, known_tx=None, on_sent=lambda _: None)
    used = nonce(rpc_url)

    recovered = relayer.anchor(b"\x02" * 32, known_tx=None, on_sent=lambda _: None)
    assert recovered.tx_hash == lost
    assert nonce(rpc_url) == used


def test_an_anchor_outside_the_scanned_range_yields_no_tx_hash(rpc_url: str) -> None:
    relayer, _ = relayer_for(rpc_url)
    lose_a_transaction(relayer, rpc_url, b"\x06" * 32)
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    late = Relayer(w3, REGISTRY, RELAYER_KEY, threading.Lock, deploy_block=w3.eth.block_number + 1)
    recovered = late.anchor(b"\x06" * 32, known_tx=None, on_sent=lambda _: None)
    assert recovered.tx_hash is None
    assert recovered.timestamp > 0


def test_concurrent_relayers_take_distinct_pending_nonces(slow_rpc_url: str) -> None:
    lock = threading.Lock()
    _, deployment = relayer_for(slow_rpc_url, lock=lock)
    workers = [
        Relayer(
            Web3(Web3.HTTPProvider(slow_rpc_url)),
            deployment.address,
            RELAYER_KEY,
            lambda: lock,
            deploy_block=deployment.block,
        )
        for _ in range(6)
    ]

    def anchor(index: int) -> Anchored:
        return workers[index].anchor(bytes([index + 1]) * 32, known_tx=None, on_sent=lambda _: None)

    with ThreadPoolExecutor(len(workers)) as pool:
        anchored = list(pool.map(anchor, range(len(workers))))
    assert len({a.tx_hash for a in anchored}) == len(workers)
    assert all(a.timestamp > 0 for a in anchored)


def test_only_the_authorised_relayer_can_anchor(rpc_url: str) -> None:
    relayer, deployment = relayer_for(rpc_url)
    stranger = Relayer(
        Web3(Web3.HTTPProvider(rpc_url)),
        deployment.address,
        STRANGER_KEY,
        threading.Lock,
        deploy_block=deployment.block,
    )
    with pytest.raises(ContractLogicError):
        stranger.anchor(b"\x03" * 32, known_tx=None, on_sent=lambda _: None)
    assert relayer.anchored_at(b"\x03" * 32) == 0


def test_workers_refuse_to_run_without_a_deployed_registry(rpc_url: str) -> None:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    with pytest.raises(ChainError, match="deploy"):
        Relayer(
            w3,
            "0x000000000000000000000000000000000000dEaD",
            RELAYER_KEY,
            threading.Lock,
            deploy_block=0,
        )


def test_the_client_verifies_anchors_with_a_plain_eth_call(rpc_url: str) -> None:
    relayer, deployment = relayer_for(rpc_url)
    anchored = relayer.anchor(b"\x04" * 32, known_tx=None, on_sent=lambda _: None)
    assert anchored_at(rpc_url, deployment.address, b"\x04" * 32) == anchored.timestamp
    assert anchored_at(rpc_url, deployment.address, b"\x05" * 32) == 0
