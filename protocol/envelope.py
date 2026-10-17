"""HPKE (RFC 9180) envelopes bound to a tenant, a document and a protocol version."""

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hpke, serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from protocol import cbor

VERSION: Final = 1
KEY_ID_BYTES: Final = 8
SUITE: Final = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)
_MIN_SEALED_BYTES: Final = hpke.KEM.X25519.enc_length() + 16


class EnvelopeError(ValueError):
    pass


class StaleKeyError(EnvelopeError):
    """The envelope was sealed to a key this enclave no longer holds."""


@dataclass(frozen=True)
class Binding:
    tenant_id: str
    document_id: UUID

    def aad(self, purpose: str) -> bytes:
        return cbor.encode(
            [f"legaldocs/{purpose}/v{VERSION}", self.tenant_id, str(self.document_id)]
        )

    def digest(self) -> bytes:
        return hashlib.sha256(self.aad("binding")).digest()


@dataclass(frozen=True)
class Envelope:
    key_id: bytes
    sealed: bytes

    def encode(self) -> bytes:
        return cbor.encode({"v": VERSION, "kid": self.key_id, "ct": self.sealed})

    @classmethod
    def decode(cls, data: bytes) -> "Envelope":
        try:
            fields = cbor.decode_map(data)
            version = cbor.field(fields, "v", int)
            key_id = cbor.field(fields, "kid", bytes)
            sealed = cbor.field(fields, "ct", bytes)
        except cbor.DecodeError as exc:
            raise EnvelopeError("malformed envelope") from exc
        if version != VERSION or len(key_id) != KEY_ID_BYTES or len(sealed) < _MIN_SEALED_BYTES:
            raise EnvelopeError("unsupported envelope")
        return cls(key_id, sealed)


def key_id(public_key: x25519.X25519PublicKey) -> bytes:
    return hashlib.sha256(raw_public_key(public_key)).digest()[:KEY_ID_BYTES]


def raw_public_key(public_key: x25519.X25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_public_key(raw: bytes) -> x25519.X25519PublicKey:
    try:
        return x25519.X25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise EnvelopeError("not an X25519 public key") from exc


# cryptography's public HPKE API is single-shot and takes only `info`. In single-shot
# mode info and aad bind equally: both feed the AEAD key, so a mismatch fails to open.
def seal(recipient: x25519.X25519PublicKey, plaintext: bytes, aad: bytes) -> Envelope:
    return Envelope(key_id(recipient), SUITE.encrypt(plaintext, recipient, info=aad))


def unseal(envelope: Envelope, recipient: x25519.X25519PrivateKey, aad: bytes) -> bytes:
    if envelope.key_id != key_id(recipient.public_key()):
        raise StaleKeyError("envelope sealed to a different key")
    try:
        return SUITE.decrypt(envelope.sealed, recipient, info=aad)
    except (InvalidTag, ValueError) as exc:
        raise EnvelopeError("envelope does not open") from exc
