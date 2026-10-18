import json
import logging
import os
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlsplit

from enclave.nsm_client import UnixSocketNsm
from enclave.service import EnclaveError, EnclaveKeys, EnclaveService
from enclave.tls import TlsIdentity, is_parent
from enclave.workers import WorkerPool

READ_CHUNK_BYTES: Final = 64 * 1024

log = logging.getLogger("enclave.http")


class EnclaveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        service: EnclaveService,
        tls: TlsIdentity,
        *,
        max_connections: int = 32,
        max_body_bytes: int = 8 * 1024 * 1024,
        io_timeout: float = 10.0,
    ) -> None:
        self.service = service
        self.tls = tls
        self.max_body_bytes = max_body_bytes
        self.io_timeout = io_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        # finish_request builds each handler itself, passing the connection's certificate.
        super().__init__(address, EnclaveHandler)  # type: ignore[arg-type]

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            log.warning("refused connection: at capacity")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def finish_request(self, request: Any, client_address: Any) -> None:
        request.settimeout(self.io_timeout)
        context, certificate = self.tls.current()
        try:
            connection = context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as exc:
            log.info("TLS handshake failed: %s", type(exc).__name__)
            return
        with connection:
            if not is_parent(connection.getpeercert()):
                log.warning("refused client certificate without the parent name")
                return
            EnclaveHandler(connection, client_address, self, certificate)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.error("connection failed: %s", type(sys.exc_info()[1]).__name__)


class EnclaveHandler(BaseHTTPRequestHandler):
    server: EnclaveHTTPServer
    server_version = "enclave"
    sys_version = ""

    def __init__(
        self, request: Any, client_address: Any, server: EnclaveHTTPServer, certificate: bytes
    ) -> None:
        self.certificate = certificate
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/health":
            self._reply(200, b'{"status":"ok"}', "application/json")
        elif url.path == "/attestation":
            nonces = parse_qs(url.query).get("nonce", [])
            try:
                nonce = bytes.fromhex(nonces[0]) if len(nonces) == 1 else b""
            except ValueError:
                nonce = b""
            self._run(lambda: self.server.service.attestation(nonce, self.certificate))
        else:
            self._error(404, "not_found")

    def do_POST(self) -> None:
        if self.path != "/process":
            self._error(404, "not_found")
            return
        body = self._read_body()
        if body is not None:
            self._run(lambda: self.server.service.process(body))

    def _run(self, action: Callable[[], bytes]) -> None:
        try:
            payload = action()
        except EnclaveError as exc:
            self._error(exc.status, exc.code)
        except Exception as exc:
            log.error("internal error: %s", type(exc).__name__)
            self._error(500, "internal")
        else:
            self._reply(200, payload, "application/cbor")

    def _read_body(self) -> bytes | None:
        length = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not length.isdigit():
            self._error(411, "length_required")
            return None
        remaining = int(length)
        if remaining > self.server.max_body_bytes:
            self._error(413, "too_large")
            return None
        deadline = time.monotonic() + self.server.io_timeout
        body = bytearray()
        while remaining:
            if time.monotonic() > deadline:
                raise TimeoutError("request body too slow")
            chunk = self.rfile.read1(min(remaining, READ_CHUNK_BYTES))
            if not chunk:
                raise ConnectionError("request body truncated")
            body += chunk
            remaining -= len(chunk)
        return bytes(body)

    def _error(self, status: int, code: str) -> None:
        self._reply(status, json.dumps({"error": code}).encode(), "application/json")

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (TimeoutError, ConnectionError, ssl.SSLError) as exc:
            log.info("dropped connection: %s", type(exc).__name__)
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    extract = WorkerPool(
        workers=int(os.environ.get("EXTRACT_WORKERS", "2")),
        deadline=float(os.environ.get("PROCESS_DEADLINE_SECONDS", "10")),
    )
    nsm = UnixSocketNsm(os.environ.get("NSM_SOCKET", "/run/nsm/nsm.sock"))
    service = EnclaveService(nsm, EnclaveKeys.generate(), extract)
    tls = TlsIdentity(Path(os.environ["PARENT_CA"]).read_bytes())
    address = (os.environ.get("BIND_HOST", "0.0.0.0"), int(os.environ.get("PORT", "8443")))
    server = EnclaveHTTPServer(
        address,
        service,
        tls,
        max_connections=int(os.environ.get("MAX_CONNECTIONS", "32")),
        max_body_bytes=int(os.environ.get("MAX_BODY_BYTES", str(8 * 1024 * 1024))),
    )
    log.info("enclave ready key_id=%s pcr0=%s", service.key_id.hex(), service.pcr0.hex())
    server.serve_forever()


def healthcheck() -> None:
    socket.create_connection(("127.0.0.1", int(os.environ.get("PORT", "8443"))), timeout=2).close()


if __name__ == "__main__":
    main()
