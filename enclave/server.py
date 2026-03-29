"""mTLS HTTP server for the enclave.

Provides /health, /attestation, and /decrypt endpoints over HTTPS
with mutual TLS authentication.
"""

import base64
import json
import logging
import os
import ssl
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

from crypto import CryptoManager
from extractor import extract_fields


# ---------------------------------------------------------------------------
# Standalone JSON logging for the enclave (no shared module with parent)
# ---------------------------------------------------------------------------

class _EnclaveJSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": "enclave",
        }
        for key in ("correlation_id", "stage", "duration_ms", "status_code", "method", "endpoint"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(entry, default=str)


def _setup_enclave_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_EnclaveJSONFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return logging.getLogger("enclave")


enclave_logger = _setup_enclave_logging()

# ---------------------------------------------------------------------------


def create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context requiring client certificate verification."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(
        certfile=os.environ.get("ENCLAVE_CERT_PATH", "/certs/enclave.crt"),
        keyfile=os.environ.get("ENCLAVE_KEY_PATH", "/certs/enclave.key"),
    )
    context.load_verify_locations(
        cafile=os.environ.get("CA_CERT_PATH", "/certs/ca.crt")
    )
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _load_signing_key():
    """Load the enclave mTLS private key for attestation signing."""
    key_path = os.environ.get("ENCLAVE_KEY_PATH", "/certs/enclave.key")
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _load_certificate() -> str:
    """Load the enclave mTLS certificate as a PEM string."""
    cert_path = os.environ.get("ENCLAVE_CERT_PATH", "/certs/enclave.crt")
    with open(cert_path, "rb") as f:
        return f.read().decode("utf-8")


class EnclaveHTTPServer(HTTPServer):
    """Custom HTTP server that holds a CryptoManager instance."""

    def __init__(self, server_address, handler_class, crypto_manager: CryptoManager):
        self.crypto = crypto_manager
        super().__init__(server_address, handler_class)


# Stable enclave ID generated once at module import (persists for server lifetime)
_ENCLAVE_ID = str(uuid.uuid4())


class EnclaveHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the enclave server."""

    server: EnclaveHTTPServer  # type hint for IDE support

    def do_GET(self) -> None:
        if self.path == "/health":
            self._handle_health()
        elif self.path == "/attestation":
            self._handle_attestation()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/decrypt":
            self._handle_decrypt()
        elif self.path == "/process":
            self._handle_process()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_health(self) -> None:
        response = json.dumps({"status": "healthy", "service": "enclave"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response.encode())

    def _handle_attestation(self) -> None:
        payload = {
            "enclave_id": _ENCLAVE_ID,
            "public_key": self.server.crypto.get_public_key_pem().decode("utf-8"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "platform": "docker-simulation",
        }

        # Serialize payload to deterministic JSON bytes for signing
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

        # Sign with enclave mTLS private key using PKCS1v15 + SHA256
        signing_key = _load_signing_key()
        signature = signing_key.sign(
            payload_bytes,
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )

        certificate = _load_certificate()

        response = json.dumps({
            "payload": payload,
            "signature": base64.b64encode(signature).decode("utf-8"),
            "certificate": certificate,
        })

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response.encode())

        enclave_logger.info(
            "Attestation served",
            extra={"stage": "enclave.attestation", "endpoint": "/attestation", "method": "GET", "status_code": 200},
        )

    def _handle_process(self) -> None:
        """Handle POST /process: decrypt, extract fields, encrypt results."""
        correlation_id = self.headers.get("X-Correlation-ID")
        start = time.monotonic()
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            encrypted_blob = self.rfile.read(content_length)

            # Decrypt the document
            plaintext = self.server.crypto.decrypt_document(encrypted_blob)

            # Extract fields from plaintext
            results = extract_fields(plaintext.decode("utf-8"))

            # Serialize results to JSON bytes
            json_bytes = json.dumps(results).encode("utf-8")

            # Encrypt results with enclave's public key
            encrypted_results = CryptoManager.encrypt_document(
                json_bytes, self.server.crypto.get_public_key_pem()
            )

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(encrypted_results)

            duration_ms = (time.monotonic() - start) * 1000
            enclave_logger.info(
                "Document processed",
                extra={
                    "correlation_id": correlation_id,
                    "stage": "enclave.process",
                    "endpoint": "/process",
                    "method": "POST",
                    "duration_ms": round(duration_ms, 2),
                    "status_code": 200,
                },
            )
        except Exception as e:
            error_response = json.dumps({"error": str(e)})
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_response.encode())

            duration_ms = (time.monotonic() - start) * 1000
            enclave_logger.error(
                "Process failed",
                extra={
                    "correlation_id": correlation_id,
                    "stage": "enclave.process",
                    "endpoint": "/process",
                    "method": "POST",
                    "duration_ms": round(duration_ms, 2),
                    "status_code": 400,
                },
            )

    def _handle_decrypt(self) -> None:
        correlation_id = self.headers.get("X-Correlation-ID")
        start = time.monotonic()
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            encrypted_blob = self.rfile.read(content_length)
            decrypted = self.server.crypto.decrypt_document(encrypted_blob)

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            self.wfile.write(decrypted)

            duration_ms = (time.monotonic() - start) * 1000
            enclave_logger.info(
                "Decrypt completed",
                extra={
                    "correlation_id": correlation_id,
                    "stage": "enclave.decrypt",
                    "endpoint": "/decrypt",
                    "method": "POST",
                    "duration_ms": round(duration_ms, 2),
                    "status_code": 200,
                },
            )
        except Exception as e:
            error_response = json.dumps({"error": str(e)})
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_response.encode())

            duration_ms = (time.monotonic() - start) * 1000
            enclave_logger.error(
                "Decrypt failed",
                extra={
                    "correlation_id": correlation_id,
                    "stage": "enclave.decrypt",
                    "endpoint": "/decrypt",
                    "method": "POST",
                    "duration_ms": round(duration_ms, 2),
                    "status_code": 400,
                },
            )

    def log_message(self, format: str, *args) -> None:
        """Override default stderr logging to use structured logger."""
        enclave_logger.info(
            "HTTP %s %s %s" % args,
            extra={"stage": "enclave.http"},
        )


def run_server(host: str = "0.0.0.0", port: int = 8443) -> None:
    """Start the mTLS HTTPS server."""
    crypto_manager = CryptoManager()
    server = EnclaveHTTPServer((host, port), EnclaveHandler, crypto_manager)
    ssl_context = create_ssl_context()
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    enclave_logger.info(f"Enclave server running on https://{host}:{port}")
    enclave_logger.info(f"Enclave ID: {_ENCLAVE_ID}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
