"""The parent's channel to the enclave: mutual TLS, with the enclave's certificate pinned
only after a fresh attestation binds it to the expected PCR0.

The enclave mints its own certificate at boot, so no CA the parent controls can vouch for
it, and the parent cannot impersonate it to anyone.
"""

import hashlib
import hmac
import http.client
import json
import logging
import os
import ssl
import threading
from pathlib import Path
from typing import Protocol

from protocol.attestation import ENCLAVE_TLS_NAME, AttestationError, Policy, verify_enclave
from protocol.messages import ProcessRequest, ProcessResponse

log = logging.getLogger("parent.enclave")


class EnclaveUnavailable(RuntimeError):
    pass


class EnclaveRejected(RuntimeError):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"enclave rejected the request: {code}")
        self.code = code
        self.status = status


class EnclaveLink(Protocol):
    def attestation(self, nonce: bytes) -> bytes: ...

    def process(self, request: ProcessRequest) -> ProcessResponse: ...


class AttestedTlsLink:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_cert: Path,
        client_key: Path,
        trust_anchor: Path,
        expected_pcr0: str,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._client_cert = client_cert
        self._client_key = client_key
        self._trust_anchor = trust_anchor
        self._expected_pcr0 = expected_pcr0
        self._timeout = timeout
        self._lock = threading.Lock()
        self._pinned: ssl.SSLContext | None = None

    def attestation(self, nonce: bytes) -> bytes:
        return self._request("GET", f"/attestation?nonce={nonce.hex()}")

    def process(self, request: ProcessRequest) -> ProcessResponse:
        return ProcessResponse.decode(self._request("POST", "/process", request.encode()))

    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        context = self._context()
        try:
            status, payload, _ = self._send(context, method, path, body)
        except ssl.SSLCertVerificationError:
            self._forget(context)
            try:
                status, payload, _ = self._send(self._context(), method, path, body)
            except ssl.SSLCertVerificationError as exc:
                raise EnclaveUnavailable("enclave certificate is not the attested one") from exc
        if status != 200:
            raise EnclaveRejected(_error_code(payload), status)
        return payload

    def _send(
        self, context: ssl.SSLContext, method: str, path: str, body: bytes | None
    ) -> tuple[int, bytes, bytes]:
        connection = _Connection(self._host, self._port, context, self._timeout)
        try:
            connection.connect()
            peer = connection.sock.getpeercert(binary_form=True) or b""
            headers = {"Content-Type": "application/cbor"} if body is not None else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), peer
        except ssl.SSLCertVerificationError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise EnclaveUnavailable(f"cannot reach the enclave: {type(exc).__name__}") from exc
        finally:
            connection.close()

    def _context(self) -> ssl.SSLContext:
        with self._lock:
            if self._pinned is None:
                self._pinned = self._attest_and_pin()
            return self._pinned

    def _forget(self, context: ssl.SSLContext) -> None:
        with self._lock:
            if self._pinned is context:
                self._pinned = None

    def _attest_and_pin(self) -> ssl.SSLContext:
        probe = self._client_context()
        probe.check_hostname = False
        probe.verify_mode = ssl.CERT_NONE
        nonce = os.urandom(32)
        status, document, peer = self._send(probe, "GET", f"/attestation?nonce={nonce.hex()}", None)
        if status != 200 or not peer:
            raise EnclaveUnavailable("enclave did not return an attestation")

        policy = Policy.from_pem(self._trust_anchor.read_bytes(), self._expected_pcr0)
        try:
            identity = verify_enclave(document, policy, nonce)
        except AttestationError as exc:
            log.error("enclave failed attestation: %s", exc)
            raise EnclaveUnavailable("enclave failed attestation") from exc
        if not hmac.compare_digest(hashlib.sha256(peer).digest(), identity.tls_cert_sha256):
            log.error("enclave TLS certificate is not the one its attestation binds")
            raise EnclaveUnavailable("enclave TLS certificate is not attested")

        pinned = self._client_context()
        pinned.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(peer))
        log.info(
            "pinned attested enclave key_id=%s pcr0=%s",
            identity.key_id.hex(),
            identity.pcr0.hex(),
        )
        return pinned

    def _client_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        context.load_cert_chain(self._client_cert, self._client_key)
        return context


class _Connection(http.client.HTTPSConnection):
    """Verifies the enclave's attested name whatever address it was reached at."""

    def __init__(self, host: str, port: int, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._tls = context

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)
        self.sock = self._tls.wrap_socket(self.sock, server_hostname=ENCLAVE_TLS_NAME)


def _error_code(payload: bytes) -> str:
    try:
        code = json.loads(payload).get("error")
    except (ValueError, AttributeError):
        return "unknown"
    return code if isinstance(code, str) else "unknown"
