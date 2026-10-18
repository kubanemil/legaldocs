from collections.abc import Callable
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import x25519

from protocol import cbor, envelope
from protocol.envelope import Binding, Envelope, EnvelopeError, StaleKeyError


@pytest.fixture
def key() -> x25519.X25519PrivateKey:
    return x25519.X25519PrivateKey.generate()


@pytest.fixture
def binding() -> Binding:
    return Binding("acme", uuid4())


def test_a_sealed_document_opens_with_the_recipient_key_and_the_same_binding(
    key: x25519.X25519PrivateKey, binding: Binding
) -> None:
    sealed = envelope.seal(key.public_key(), b"contract text", binding.aad("request"))
    opened = envelope.unseal(Envelope.decode(sealed.encode()), key, binding.aad("request"))
    assert opened == b"contract text"


@pytest.mark.parametrize(
    "tampered",
    [
        lambda b: Binding("globex", b.document_id).aad("request"),
        lambda b: Binding(b.tenant_id, uuid4()).aad("request"),
        lambda b: b.aad("result"),
        lambda b: cbor.encode(["legaldocs/request/v2", b.tenant_id, str(b.document_id)]),
    ],
    ids=["other tenant", "other document", "other purpose", "other version"],
)
def test_a_tampered_aad_fails_decryption(
    key: x25519.X25519PrivateKey, binding: Binding, tampered: Callable[[Binding], bytes]
) -> None:
    sealed = envelope.seal(key.public_key(), b"contract text", binding.aad("request"))
    with pytest.raises(EnvelopeError):
        envelope.unseal(sealed, key, tampered(binding))


def test_a_tampered_ciphertext_fails_decryption(
    key: x25519.X25519PrivateKey, binding: Binding
) -> None:
    sealed = envelope.seal(key.public_key(), b"contract text", binding.aad("request"))
    flipped = sealed.sealed[:-1] + bytes([sealed.sealed[-1] ^ 1])
    with pytest.raises(EnvelopeError):
        envelope.unseal(Envelope(sealed.key_id, flipped), key, binding.aad("request"))


def test_an_envelope_for_a_previous_key_is_reported_as_stale(
    key: x25519.X25519PrivateKey, binding: Binding
) -> None:
    sealed = envelope.seal(key.public_key(), b"contract text", binding.aad("request"))
    with pytest.raises(StaleKeyError):
        envelope.unseal(sealed, x25519.X25519PrivateKey.generate(), binding.aad("request"))


@pytest.mark.parametrize(
    "data",
    [
        b"not cbor at all",
        cbor.encode({"v": 2, "kid": bytes(8), "ct": bytes(64)}),
        cbor.encode({"v": 1, "kid": bytes(4), "ct": bytes(64)}),
        cbor.encode({"v": 1, "kid": bytes(8), "ct": bytes(10)}),
        cbor.encode({"v": 1, "kid": bytes(8), "ct": bytes(64)}) + b"\x00",
        bytes.fromhex("a3617601617601626374f6"),
    ],
    ids=["garbage", "version", "key id", "short", "trailing bytes", "duplicate keys"],
)
def test_malformed_envelopes_are_rejected(data: bytes) -> None:
    with pytest.raises(EnvelopeError):
        Envelope.decode(data)


def test_a_low_order_reply_key_is_refused() -> None:
    with pytest.raises(EnvelopeError):
        envelope.load_public_key(bytes(32))
