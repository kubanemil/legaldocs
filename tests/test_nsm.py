import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from enclave.nsm_client import UnixSocketNsm
from nsm.device import MockNsm
from nsm.server import NsmServer
from protocol import nsm_api
from protocol.attestation import Policy, verify


@pytest.fixture
def socket_path(nsm: MockNsm, tmp_path: Path) -> Iterator[str]:
    path = str(tmp_path / "nsm.sock")
    server = NsmServer(path, nsm)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield path
    server.shutdown()
    server.server_close()


def test_the_enclave_gets_attestations_through_the_nsm_socket(
    socket_path: str, policy: Policy, pcrs: dict[int, bytes]
) -> None:
    device = UnixSocketNsm(socket_path)
    nonce = os.urandom(32)
    document = verify(
        device.attest(public_key=bytes(32), user_data=b"hi", nonce=nonce), policy, nonce
    )
    assert document.user_data == b"hi"
    assert device.describe_pcr(0) == pcrs[0]


def test_the_nsm_reports_bad_requests_as_nsm_errors(socket_path: str) -> None:
    device = UnixSocketNsm(socket_path)
    with pytest.raises(nsm_api.NsmError, match="InvalidArgument"):
        device.attest(public_key=b"", user_data=b"", nonce=bytes(nsm_api.MAX_NONCE_BYTES + 1))
    with pytest.raises(nsm_api.NsmError, match="InvalidIndex"):
        device.describe_pcr(9)


def test_the_nsm_ignores_requests_it_does_not_know(nsm: MockNsm) -> None:
    assert nsm.handle({"Unlock": {}}) == {"Error": "InvalidOperation"}
    assert nsm.handle({"Attestation": {"nonce": "text"}}) == {"Error": "InvalidArgument"}
