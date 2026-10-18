"""The enclave's work: attest its keys, open document envelopes, seal results to the client.

Keys are generated at boot and never leave this process. A restart re-keys, and
envelopes sealed to the old key fail with `stale_key` (see README, "Key lifetime").
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from enclave import workers
from enclave.nsm_client import Nsm
from protocol import envelope, receipt
from protocol.attestation import enclave_user_data
from protocol.envelope import Envelope
from protocol.messages import DocumentRequest, ProcessRequest, ProcessResponse

MIN_NONCE_BYTES: Final = 16
MAX_NONCE_BYTES: Final = 64

log = logging.getLogger("enclave")


class EnclaveError(Exception):
    """A failure the enclave reports to its caller as a fixed code and nothing more."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class EnclaveKeys:
    encryption: x25519.X25519PrivateKey
    signing: ed25519.Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "EnclaveKeys":
        return cls(x25519.X25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate())


class EnclaveService:
    def __init__(
        self, nsm: Nsm, keys: EnclaveKeys, extract: Callable[[str], dict[str, Any]]
    ) -> None:
        self._nsm = nsm
        self._keys = keys
        self._extract = extract
        self.pcr0 = nsm.describe_pcr(0)
        self.key_id = envelope.key_id(keys.encryption.public_key())
        self._public_key = envelope.raw_public_key(keys.encryption.public_key())

    def attestation(self, nonce: bytes, tls_certificate: bytes) -> bytes:
        """Attest this enclave's keys and the TLS certificate the caller is connected through."""
        if not MIN_NONCE_BYTES <= len(nonce) <= MAX_NONCE_BYTES:
            raise EnclaveError("bad_request")
        user_data = enclave_user_data(self._keys.signing.public_key(), tls_certificate)
        return self._nsm.attest(public_key=self._public_key, user_data=user_data, nonce=nonce)

    def process(self, body: bytes) -> bytes:
        try:
            request = ProcessRequest.decode(body)
            sealed = Envelope.decode(request.envelope)
            opened = envelope.unseal(sealed, self._keys.encryption, request.binding.aad("request"))
            document = DocumentRequest.decode(opened)
            text = document.content.decode("utf-8")
            reply_key = envelope.load_public_key(document.reply_key)
        except envelope.StaleKeyError:
            raise EnclaveError("stale_key", 409) from None
        except ValueError as exc:
            log.info("rejected request: %s", type(exc).__name__)
            raise EnclaveError("bad_request") from None

        try:
            fields = self._extract(text)
        except workers.Timeout:
            raise EnclaveError("processing_timeout", 422) from None
        except workers.Busy:
            raise EnclaveError("busy", 503) from None
        result = json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()
        signed = receipt.sign(
            receipt.Receipt(
                binding=request.binding.digest(),
                input_hash=receipt.commit(document.salt, document.content),
                output_hash=receipt.commit(document.salt, result),
                pcr0=self.pcr0,
                timestamp_ms=int(time.time() * 1000),
            ),
            self._keys.signing,
        )
        sealed_result = envelope.seal(reply_key, result, request.binding.aad("result"))
        return ProcessResponse(sealed_result.encode(), signed).encode()
