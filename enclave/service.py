"""The enclave's work: attest its keys, open document envelopes, seal results to the client.

Keys are generated at boot and never leave this process. A restart re-keys, and
envelopes sealed to the old key fail with `stale_key` (see README, "Key lifetime").
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, x25519

from enclave.extractor import extract_fields
from enclave.nsm_client import Nsm
from enclave.tls import self_signed_certificate
from protocol import envelope, receipt
from protocol.attestation import enclave_user_data
from protocol.envelope import Envelope
from protocol.messages import DocumentRequest, ProcessRequest, ProcessResponse

MIN_NONCE_BYTES: Final = 16
MAX_NONCE_BYTES: Final = 64

log = logging.getLogger("enclave")


class EnclaveError(Exception):
    """A failure the enclave reports to its caller as a fixed code and nothing more."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


BAD_REQUEST: Final = ("bad_request", 400)
STALE_KEY: Final = ("stale_key", 409)


@dataclass(frozen=True)
class EnclaveKeys:
    encryption: x25519.X25519PrivateKey
    signing: ed25519.Ed25519PrivateKey
    tls_key: ec.EllipticCurvePrivateKey
    tls_certificate: x509.Certificate

    @classmethod
    def generate(cls) -> "EnclaveKeys":
        tls_key = ec.generate_private_key(ec.SECP256R1())
        return cls(
            encryption=x25519.X25519PrivateKey.generate(),
            signing=ed25519.Ed25519PrivateKey.generate(),
            tls_key=tls_key,
            tls_certificate=self_signed_certificate(tls_key),
        )


class EnclaveService:
    def __init__(self, nsm: Nsm, keys: EnclaveKeys) -> None:
        self._nsm = nsm
        self._keys = keys
        self.pcr0 = nsm.describe_pcr(0)
        self.key_id = envelope.key_id(keys.encryption.public_key())
        self._public_key = envelope.raw_public_key(keys.encryption.public_key())
        self._user_data = enclave_user_data(
            keys.signing.public_key(),
            keys.tls_certificate.public_bytes(serialization.Encoding.DER),
        )

    def attestation(self, nonce: bytes) -> bytes:
        if not MIN_NONCE_BYTES <= len(nonce) <= MAX_NONCE_BYTES:
            raise EnclaveError(*BAD_REQUEST)
        return self._nsm.attest(public_key=self._public_key, user_data=self._user_data, nonce=nonce)

    def process(self, body: bytes) -> bytes:
        try:
            request = ProcessRequest.decode(body)
            sealed = Envelope.decode(request.envelope)
            opened = envelope.unseal(sealed, self._keys.encryption, request.binding.aad("request"))
            document = DocumentRequest.decode(opened)
            text = document.content.decode("utf-8")
            reply_key = envelope.load_public_key(document.reply_key)
        except envelope.StaleKeyError:
            raise EnclaveError(*STALE_KEY) from None
        except ValueError as exc:
            log.info("rejected request: %s", type(exc).__name__)
            raise EnclaveError(*BAD_REQUEST) from None

        result = json.dumps(extract_fields(text), sort_keys=True, ensure_ascii=False).encode()
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
