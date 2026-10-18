import json
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.x509.oid import NameOID

from enclave.extractor import extract_fields
from enclave.server import EnclaveHTTPServer
from enclave.service import EnclaveKeys, EnclaveService
from enclave.tls import TlsIdentity
from enclave.workers import Busy, WorkerPool
from nsm.device import MockNsm
from parent.enclave_link import AttestedTlsLink, EnclaveRejected, EnclaveUnavailable
from protocol import envelope
from protocol.envelope import Binding
from protocol.messages import DocumentRequest, ProcessRequest
from scripts.certs import write as write_parent_identity
from tests.slow_job import stall_on_slow


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("certs")
    write_parent_identity(directory)
    return directory


@pytest.fixture
def trust_anchor(nsm: MockNsm, tmp_path: Path) -> Path:
    path = tmp_path / "nsm-root.pem"
    path.write_bytes(nsm.root_pem)
    return path


type StartEnclave = Callable[..., tuple[EnclaveHTTPServer, TlsIdentity]]


@pytest.fixture
def start_enclave(nsm: MockNsm, certs: Path) -> Iterator[StartEnclave]:
    servers: list[EnclaveHTTPServer] = []

    def start(
        port: int = 0,
        *,
        keys: EnclaveKeys | None = None,
        tls: TlsIdentity | None = None,
        extract: Callable[[str], dict[str, Any]] = extract_fields,
        **limits: Any,
    ) -> tuple[EnclaveHTTPServer, TlsIdentity]:
        tls = tls or TlsIdentity((certs / "parent-ca.crt").read_bytes())
        service = EnclaveService(nsm, keys or EnclaveKeys.generate(), extract)
        server = EnclaveHTTPServer(("127.0.0.1", port), service, tls, **limits)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server, tls

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def link_to(
    server: EnclaveHTTPServer, certs: Path, trust_anchor: Path, pcr0: bytes
) -> AttestedTlsLink:
    return AttestedTlsLink(
        "127.0.0.1",
        server.server_address[1],
        client_cert=certs / "parent.crt",
        client_key=certs / "parent.key",
        trust_anchor=trust_anchor,
        expected_pcr0=pcr0.hex(),
        timeout=5,
    )


def parent_tls(certs: Path, tls: TlsIdentity) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_cert_chain(certs / "parent.crt", certs / "parent.key")
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(tls.current()[1]))
    return context


def sealed_request(keys: EnclaveKeys, content: bytes) -> ProcessRequest:
    binding = Binding("acme", uuid4())
    reply_key = envelope.raw_public_key(x25519.X25519PrivateKey.generate().public_key())
    document = DocumentRequest(content, reply_key, bytes(32))
    sealed = envelope.seal(keys.encryption.public_key(), document.encode(), binding.aad("request"))
    return ProcessRequest(binding, sealed.encode())


def raw_exchange(port: int, context: ssl.SSLContext, data: bytes) -> bytes:
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        context.wrap_socket(raw) as tls,
    ):
        tls.sendall(data)
        response = b""
        while chunk := tls.recv(4096):
            response += chunk
        return response


def test_the_parent_pins_the_enclave_after_checking_its_attestation(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path, pcrs: dict[int, bytes]
) -> None:
    server, _ = start_enclave()
    link = link_to(server, certs, trust_anchor, pcrs[0])
    assert link.attestation(bytes(32))
    with pytest.raises(EnclaveRejected) as rejected:
        link.process(ProcessRequest(Binding("acme", uuid4()), b"not an envelope"))
    assert (rejected.value.code, rejected.value.status) == ("bad_request", 400)


def test_the_parent_refuses_an_enclave_running_other_code(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path
) -> None:
    server, _ = start_enclave()
    with pytest.raises(EnclaveUnavailable, match="attestation"):
        link_to(server, certs, trust_anchor, bytes(48)).attestation(bytes(32))


def test_the_parent_re_attests_when_the_enclave_restarts_with_new_keys(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path, pcrs: dict[int, bytes]
) -> None:
    first, _ = start_enclave()
    port = first.server_address[1]
    link = link_to(first, certs, trust_anchor, pcrs[0])
    link.attestation(bytes(32))
    first.shutdown()
    first.server_close()

    start_enclave(port)
    assert link.attestation(bytes(32))


def test_only_the_parent_certificate_can_open_a_connection(
    start_enclave: StartEnclave, certs: Path
) -> None:
    server, tls = start_enclave()
    anonymous = parent_tls(certs, tls)
    anonymous.load_cert_chain(*_self_signed_client(certs.parent))
    with pytest.raises((ssl.SSLError, ConnectionError)):
        raw_exchange(server.server_address[1], anonymous, b"GET /health HTTP/1.0\r\n\r\n")


def test_errors_carry_a_code_and_nothing_else(start_enclave: StartEnclave, certs: Path) -> None:
    server, tls = start_enclave()
    request = b"POST /process HTTP/1.0\r\nContent-Length: 5\r\n\r\nhello"
    response = raw_exchange(server.server_address[1], parent_tls(certs, tls), request)
    head, body = response.split(b"\r\n\r\n", 1)
    assert head.startswith(b"HTTP/1.0 400")
    assert json.loads(body) == {"error": "bad_request"}


@pytest.mark.parametrize(
    ("request_head", "status"),
    [
        (b"POST /process HTTP/1.0\r\nContent-Length: 999999999\r\n\r\n", b"413"),
        (b"POST /process HTTP/1.0\r\nTransfer-Encoding: chunked\r\n\r\n", b"411"),
        (b"POST /process HTTP/1.0\r\n\r\n", b"411"),
    ],
    ids=["too large", "chunked", "no length"],
)
def test_bodies_without_a_bounded_length_are_refused_before_reading(
    start_enclave: StartEnclave, certs: Path, request_head: bytes, status: bytes
) -> None:
    server, tls = start_enclave()
    response = raw_exchange(server.server_address[1], parent_tls(certs, tls), request_head)
    assert response.split(b" ")[1] == status


def test_a_stalled_client_neither_blocks_others_nor_holds_its_slot(
    start_enclave: StartEnclave, certs: Path
) -> None:
    server, tls = start_enclave(io_timeout=1.0, max_connections=2)
    port = server.server_address[1]
    context = parent_tls(certs, tls)
    with socket.create_connection(("127.0.0.1", port)) as raw, context.wrap_socket(raw) as stalled:
        stalled.sendall(b"POST /process HTTP/1.0\r\nContent-Length: 100\r\n\r\npartial")
        started = time.monotonic()
        health = raw_exchange(port, context, b"GET /health HTTP/1.0\r\n\r\n")
        assert health.startswith(b"HTTP/1.0 200")
        assert time.monotonic() - started < 1.0
        stalled.settimeout(5)
        assert stalled.recv(1) == b""


def _self_signed_client(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "parent")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("parent")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "impostor.crt", directory / "impostor.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_the_enclave_rotates_its_certificate_and_the_parent_re_attests(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path, pcrs: dict[int, bytes]
) -> None:
    tls = TlsIdentity((certs / "parent-ca.crt").read_bytes(), lifetime=timedelta(seconds=2))
    server, _ = start_enclave(tls=tls)
    link = link_to(server, certs, trust_anchor, pcrs[0])
    link.attestation(bytes(32))
    before = tls.current()[1]

    time.sleep(1.1)
    assert link.attestation(bytes(32))
    assert tls.current()[1] != before


def test_the_parent_refuses_an_expired_enclave_certificate(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path, pcrs: dict[int, bytes]
) -> None:
    two_days_ago = datetime.now(UTC) - timedelta(days=2)
    stale = TlsIdentity((certs / "parent-ca.crt").read_bytes(), clock=lambda: two_days_ago)
    server, _ = start_enclave(tls=stale)
    with pytest.raises(EnclaveUnavailable, match="not currently valid"):
        link_to(server, certs, trust_anchor, pcrs[0]).attestation(bytes(32))


def test_processing_past_the_deadline_is_stopped_and_frees_its_slot(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path, pcrs: dict[int, bytes]
) -> None:
    keys = EnclaveKeys.generate()
    pool = WorkerPool(workers=1, deadline=1.0, job=stall_on_slow)
    try:
        server, _ = start_enclave(keys=keys, extract=pool, max_connections=1)
        link = link_to(server, certs, trust_anchor, pcrs[0])

        started = time.monotonic()
        with pytest.raises(EnclaveRejected) as overran:
            link.process(sealed_request(keys, b"slow " + b"a" * 1000))
        assert (overran.value.code, overran.value.status) == ("processing_timeout", 422)
        assert time.monotonic() - started < 5

        link.process(sealed_request(keys, b"Agreement between Foo Inc. and Bar LLC."))
    finally:
        pool.close()


def always_busy(text: str) -> dict[str, Any]:
    raise Busy


def test_a_busy_enclave_is_retried_rather_than_failing_the_document(
    start_enclave: StartEnclave, certs: Path, trust_anchor: Path, pcrs: dict[int, bytes]
) -> None:
    keys = EnclaveKeys.generate()
    server, _ = start_enclave(keys=keys, extract=always_busy)
    with pytest.raises(EnclaveUnavailable, match="busy"):
        link_to(server, certs, trust_anchor, pcrs[0]).process(sealed_request(keys, b"text"))
