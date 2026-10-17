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
from parent.chain import Anchored, ChainError, Relayer, deploy

ROOT = Path(__file__).resolve().parent.parent
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
RELAYER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
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
    url: str, key: str = RELAYER_KEY, lock: threading.Lock | None = None
) -> tuple[Relayer, str]:
    w3 = Web3(Web3.HTTPProvider(url))
    registry = deploy(w3, OWNER_KEY, Account.from_key(RELAYER_KEY).address)
    shared = lock or threading.Lock()
    return Relayer(w3, registry, key, lambda: shared), registry


def nonce(url: str) -> int:
    return Web3(Web3.HTTPProvider(url)).eth.get_transaction_count(
        Account.from_key(RELAYER_KEY).address, "pending"
    )


def test_the_registry_lands_at_the_address_compose_is_configured_with(rpc_url: str) -> None:
    _, registry = relayer_for(rpc_url)
    assert registry in (ROOT / "docker-compose.yml").read_text()


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


def test_a_relayer_that_lost_its_tx_hash_finds_the_anchor_on_chain(rpc_url: str) -> None:
    relayer, _ = relayer_for(rpc_url)

    lost: list[str] = []

    def crash(tx_hash: str) -> None:
        lost.append(tx_hash)
        raise RuntimeError("worker died before saving the tx hash")

    with pytest.raises(RuntimeError):
        relayer.anchor(b"\x02" * 32, known_tx=None, on_sent=crash)
    Web3(Web3.HTTPProvider(rpc_url)).eth.wait_for_transaction_receipt(HexStr(lost[0]))
    used = nonce(rpc_url)

    recovered = relayer.anchor(b"\x02" * 32, known_tx=None, on_sent=crash)
    assert recovered.tx_hash == lost[0]
    assert nonce(rpc_url) == used


def test_concurrent_relayers_take_distinct_pending_nonces(slow_rpc_url: str) -> None:
    lock = threading.Lock()
    _, registry = relayer_for(slow_rpc_url, lock=lock)
    workers = [
        Relayer(Web3(Web3.HTTPProvider(slow_rpc_url)), registry, RELAYER_KEY, lambda: lock)
        for _ in range(6)
    ]

    def anchor(index: int) -> Anchored:
        return workers[index].anchor(bytes([index + 1]) * 32, known_tx=None, on_sent=lambda _: None)

    with ThreadPoolExecutor(len(workers)) as pool:
        anchored = list(pool.map(anchor, range(len(workers))))
    assert len({a.tx_hash for a in anchored}) == len(workers)
    assert all(a.timestamp > 0 for a in anchored)


def test_only_the_authorised_relayer_can_anchor(rpc_url: str) -> None:
    relayer, registry = relayer_for(rpc_url)
    stranger = Relayer(Web3(Web3.HTTPProvider(rpc_url)), registry, STRANGER_KEY, threading.Lock)
    with pytest.raises(ContractLogicError):
        stranger.anchor(b"\x03" * 32, known_tx=None, on_sent=lambda _: None)
    assert relayer.anchored_at(b"\x03" * 32) == 0


def test_workers_refuse_to_run_without_a_deployed_registry(rpc_url: str) -> None:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    with pytest.raises(ChainError, match="deploy"):
        Relayer(w3, "0x000000000000000000000000000000000000dEaD", RELAYER_KEY, threading.Lock)


def test_the_client_verifies_anchors_with_a_plain_eth_call(rpc_url: str) -> None:
    relayer, registry = relayer_for(rpc_url)
    anchored = relayer.anchor(b"\x04" * 32, known_tx=None, on_sent=lambda _: None)
    assert anchored_at(rpc_url, registry, b"\x04" * 32) == anchored.timestamp
    assert anchored_at(rpc_url, registry, b"\x05" * 32) == 0
