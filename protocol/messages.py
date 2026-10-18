from dataclasses import dataclass
from typing import Final
from uuid import UUID

from protocol import cbor
from protocol.envelope import Binding
from protocol.receipt import SALT_BYTES

X25519_KEY_BYTES: Final = 32


@dataclass(frozen=True)
class DocumentRequest:
    """The plaintext a client seals to the enclave. The parent never sees it."""

    content: bytes
    reply_key: bytes
    salt: bytes

    def encode(self) -> bytes:
        return cbor.encode(
            {"content": self.content, "reply_key": self.reply_key, "salt": self.salt}
        )

    @classmethod
    def decode(cls, data: bytes) -> "DocumentRequest":
        fields = cbor.decode_map(data)
        request = cls(
            content=cbor.field(fields, "content", bytes),
            reply_key=cbor.field(fields, "reply_key", bytes),
            salt=cbor.field(fields, "salt", bytes),
        )
        if len(request.reply_key) != X25519_KEY_BYTES or len(request.salt) != SALT_BYTES:
            raise cbor.DecodeError("bad reply key or salt length")
        return request


@dataclass(frozen=True)
class ProcessRequest:
    binding: Binding
    envelope: bytes

    def encode(self) -> bytes:
        return cbor.encode(
            {
                "tenant": self.binding.tenant_id,
                "document": str(self.binding.document_id),
                "envelope": self.envelope,
            }
        )

    @classmethod
    def decode(cls, data: bytes) -> "ProcessRequest":
        fields = cbor.decode_map(data)
        try:
            document_id = UUID(cbor.field(fields, "document", str))
        except ValueError as exc:
            raise cbor.DecodeError("document must be a UUID") from exc
        binding = Binding(cbor.field(fields, "tenant", str), document_id)
        return cls(binding, cbor.field(fields, "envelope", bytes))


@dataclass(frozen=True)
class ProcessResponse:
    sealed_result: bytes
    receipt: bytes

    def encode(self) -> bytes:
        return cbor.encode({"result": self.sealed_result, "receipt": self.receipt})

    @classmethod
    def decode(cls, data: bytes) -> "ProcessResponse":
        fields = cbor.decode_map(data)
        return cls(cbor.field(fields, "result", bytes), cbor.field(fields, "receipt", bytes))
