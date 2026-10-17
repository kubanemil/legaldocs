"""End-to-end checks against the compose stack. Run with ./do smoke."""

import base64
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from client.sdk import LegalDocsClient, ProcessingFailed, Result, anchored_at
from protocol.attestation import AttestationError, verify
from scripts.demo import connect, policy

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
MARKER = "Zephyrine Quartermaine Holdings LLC"
CONTRACT = (
    f"This Agreement is entered into as of June 15, 2026, by and between {MARKER}, "
    "a Delaware company, and Halvorsen Shipping Ltd., a Norwegian company."
)
SECRETS = (MARKER, "Halvorsen", "June 15, 2026")

UNCOMPRESSED_REDIS_DUMP = (
    "redis-cli CONFIG SET rdbcompression no >/dev/null && redis-cli SAVE >/dev/null"
    " && cat /data/dump.rdb"
)
PROBE_ENCLAVE_NETWORK = """
import json, socket
results = {}
targets = [("1.1.1.1", 53), ("postgres", 5432), ("redis", 6379), ("anvil", 8545), ("nsm", 1)]
for host, port in targets:
    try:
        socket.create_connection((host, port), timeout=3).close()
        results[f"{host}:{port}"] = "open"
    except OSError as exc:
        results[f"{host}:{port}"] = type(exc).__name__
print(json.dumps(results))
"""


def compose(*args: str, stdin: bytes | None = None) -> bytes:
    done = subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"docker compose {' '.join(args)} failed: {done.stderr.decode()}")
    return done.stdout


def container_id(service: str) -> str:
    return compose("ps", "--quiet", service).decode().strip()


@pytest.fixture(scope="module")
def acme() -> LegalDocsClient:
    return connect(os.environ["SMOKE_ACME_KEY"])


@pytest.fixture(scope="module")
def globex() -> LegalDocsClient:
    return connect(os.environ["SMOKE_GLOBEX_KEY"])


@pytest.fixture(scope="module")
def processed(acme: LegalDocsClient) -> Result:
    return acme.wait(acme.submit(CONTRACT.encode()), anchored=True, timeout=120)


def test_a_document_is_processed_in_the_enclave_and_its_receipt_anchored(processed: Result) -> None:
    assert MARKER in processed.fields["parties"]
    assert "June 15, 2026" in processed.fields["dates"]
    assert processed.anchor_tx
    registry = os.environ["REGISTRY_ADDRESS"]
    assert anchored_at(os.environ["ETH_RPC_URL"], registry, processed.receipt_hash) > 0


def test_the_parent_never_sees_plaintext(processed: Result) -> None:
    database = compose(
        "exec", "-T", "postgres", "pg_dump", "--data-only", "-U", "legaldocs", "legaldocs"
    )
    assert str(processed.document_id).encode() in database
    redis = compose(
        "exec",
        "-T",
        "redis",
        "sh",
        "-c",
        UNCOMPRESSED_REDIS_DUMP,
    )
    logs = compose("logs", "--no-color")
    for secret in SECRETS:
        for name, blob in [("postgres", database), ("redis", redis), ("container logs", logs)]:
            assert secret.encode() not in blob, f"{secret!r} leaked into {name}"


def test_the_enclave_has_no_outbound_network() -> None:
    reachable = json.loads(
        compose("exec", "-T", "enclave", "python", "-", stdin=PROBE_ENCLAVE_NETWORK.encode())
    )
    assert len(reachable) == 5
    assert "open" not in reachable.values(), reachable


def test_the_nsm_has_no_network_and_the_parent_cannot_reach_its_socket() -> None:
    inspected = json.loads(subprocess.check_output(["docker", "inspect", container_id("nsm")]))
    assert inspected[0]["HostConfig"]["NetworkMode"] == "none"
    for service in ("api", "worker"):
        found = compose("exec", "-T", service, "sh", "-c", "ls /run/nsm 2>/dev/null || true")
        assert found.strip() == b"", service


def test_the_enclave_gets_no_key_material_from_the_host() -> None:
    mounts = json.loads(subprocess.check_output(["docker", "inspect", container_id("enclave")]))[0][
        "Mounts"
    ]
    destinations = {mount["Destination"] for mount in mounts}
    assert destinations == {"/run/nsm", "/etc/legaldocs/parent-ca.crt"}
    host_files = [mount["Source"] for mount in mounts if mount["Type"] == "bind"]
    assert all(source.endswith("parent-ca.crt") for source in host_files)


def test_tenant_b_cannot_read_tenant_a_documents(
    processed: Result, globex: LegalDocsClient
) -> None:
    with pytest.raises(httpx.HTTPStatusError) as denied:
        globex.status(processed.document_id)
    assert denied.value.response.status_code == 404
    assert str(processed.document_id) not in {d["id"] for d in globex.list_documents()}


def test_the_api_rejects_missing_keys_and_has_no_decrypt_endpoint() -> None:
    api = httpx.Client(base_url=os.environ["API_URL"])
    assert api.get("/documents").status_code == 401
    assert api.post("/decrypt", json={"encrypted_data": "AAAA"}).status_code == 404


def test_a_client_expecting_other_code_refuses_the_enclave() -> None:
    with pytest.raises(AttestationError, match="PCR0"):
        connect(os.environ["SMOKE_ACME_KEY"], expected_pcr0=bytes(48)).attest()


def test_a_replayed_attestation_is_rejected() -> None:
    api = httpx.Client(base_url=os.environ["API_URL"])
    headers = {"Authorization": f"Bearer {os.environ['SMOKE_ACME_KEY']}"}
    response = api.get("/attestation", params={"nonce": "11" * 32}, headers=headers)
    document = base64.b64decode(response.json()["document"])
    verify(document, policy(), bytes.fromhex("11" * 32))
    with pytest.raises(AttestationError, match="nonce"):
        verify(document, policy(), bytes.fromhex("22" * 32))


def test_an_enclave_restart_fails_queued_documents_cleanly_and_the_client_recovers(
    acme: LegalDocsClient,
) -> None:
    before_restart = acme.attest()
    compose("restart", "enclave")
    deadline = time.monotonic() + 60
    while compose("ps", "--format", "{{.Health}}", "enclave").strip() != b"healthy":
        assert time.monotonic() < deadline, "enclave did not come back"
        time.sleep(1)

    stale = acme.submit(CONTRACT.encode(), before_restart)
    with pytest.raises(ProcessingFailed) as failure:
        acme.wait(stale, timeout=60)
    assert failure.value.code == "enclave_key_rotated"
    assert MARKER in acme.process(CONTRACT.encode(), timeout=60).fields["parties"]
