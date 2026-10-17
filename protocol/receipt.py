"""Processing receipts: what the enclave signs after it handles a document.

Commitments are keyed with a salt only the client knows, so a receipt stored by the
parent, or its hash on chain, cannot be used to confirm a guessed document.
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives.asymmetric import ed25519

from protocol import cbor, cose

VERSION: Final = 1
SALT_BYTES: Final = 32


class ReceiptError(ValueError):
    pass


@dataclass(frozen=True)
class Receipt:
    binding: bytes
    input_hash: bytes
    output_hash: bytes
    pcr0: bytes
    timestamp_ms: int

    def encode(self) -> bytes:
        return cbor.encode(
            {
                "v": VERSION,
                "binding": self.binding,
                "input": self.input_hash,
                "output": self.output_hash,
                "pcr0": self.pcr0,
                "ts": self.timestamp_ms,
            }
        )

    @classmethod
    def decode(cls, data: bytes) -> "Receipt":
        fields = cbor.decode_map(data)
        if cbor.field(fields, "v", int) != VERSION:
            raise ReceiptError("unsupported receipt version")
        return cls(
            binding=cbor.field(fields, "binding", bytes),
            input_hash=cbor.field(fields, "input", bytes),
            output_hash=cbor.field(fields, "output", bytes),
            pcr0=cbor.field(fields, "pcr0", bytes),
            timestamp_ms=cbor.field(fields, "ts", int),
        )


def commit(salt: bytes, data: bytes) -> bytes:
    return hmac.digest(salt, data, "sha256")


def sign(receipt: Receipt, key: ed25519.Ed25519PrivateKey) -> bytes:
    return cose.sign(receipt.encode(), key)


def verify(signed: bytes, key: ed25519.Ed25519PublicKey) -> Receipt:
    try:
        sign1 = cose.decode(signed)
        return Receipt.decode(sign1.verify(key))
    except ValueError as exc:
        raise ReceiptError("receipt does not verify") from exc


def anchor_hash(signed: bytes) -> bytes:
    return hashlib.sha256(signed).digest()
