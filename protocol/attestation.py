"""Attestation documents in the AWS Nitro Enclaves format, and how a relying party checks them.

The verifier knows nothing about the mock: pointed at the AWS Nitro root certificate it
checks real NSM documents the same way.
"""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any, Final

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, x25519

from protocol import cbor, cose, envelope

DIGEST: Final = "SHA384"
PCR_BYTES: Final = 48
ENCLAVE_TLS_NAME: Final = "enclave"
PARENT_TLS_NAME: Final = "parent"


class AttestationError(ValueError):
    pass


@dataclass(frozen=True)
class AttestationDocument:
    module_id: str
    timestamp_ms: int
    pcrs: Mapping[int, bytes]
    certificate: bytes
    cabundle: tuple[bytes, ...]
    public_key: bytes | None = None
    user_data: bytes | None = None
    nonce: bytes | None = None
    digest: str = DIGEST

    def encode(self) -> bytes:
        return cbor.encode(
            {
                "module_id": self.module_id,
                "digest": self.digest,
                "timestamp": self.timestamp_ms,
                "pcrs": dict(self.pcrs),
                "certificate": self.certificate,
                "cabundle": list(self.cabundle),
                "public_key": self.public_key,
                "user_data": self.user_data,
                "nonce": self.nonce,
            }
        )

    @classmethod
    def decode(cls, payload: bytes) -> "AttestationDocument":
        fields = cbor.decode_map(payload)
        pcrs = cbor.field(fields, "pcrs", dict)
        cabundle = cbor.field(fields, "cabundle", list)
        if not all(isinstance(k, int) and isinstance(v, bytes) for k, v in pcrs.items()):
            raise cbor.DecodeError("pcrs must map int to bytes")
        if not cabundle or not all(isinstance(c, bytes) for c in cabundle):
            raise cbor.DecodeError("cabundle must be a non-empty list of certificates")
        return cls(
            module_id=cbor.field(fields, "module_id", str),
            digest=cbor.field(fields, "digest", str),
            timestamp_ms=cbor.field(fields, "timestamp", int),
            pcrs=pcrs,
            certificate=cbor.field(fields, "certificate", bytes),
            cabundle=tuple(cabundle),
            public_key=cbor.optional_field(fields, "public_key", bytes),
            user_data=cbor.optional_field(fields, "user_data", bytes),
            nonce=cbor.optional_field(fields, "nonce", bytes),
        )


@dataclass(frozen=True)
class Policy:
    trust_anchor: x509.Certificate
    expected_pcr0: bytes
    max_age: timedelta = timedelta(minutes=5)
    max_clock_skew: timedelta = timedelta(seconds=30)

    @classmethod
    def from_pem(cls, trust_anchor_pem: bytes, expected_pcr0_hex: str) -> "Policy":
        pcr0 = bytes.fromhex(expected_pcr0_hex)
        if len(pcr0) != PCR_BYTES:
            raise ValueError("expected PCR0 must be 48 bytes of hex")
        return cls(x509.load_pem_x509_certificate(trust_anchor_pem), pcr0)


def verify(
    document: bytes, policy: Policy, nonce: bytes, now: datetime | None = None
) -> AttestationDocument:
    """Check a document against the policy. Raises AttestationError and nothing else."""
    try:
        return _verify(document, policy, nonce, now or datetime.now(UTC))
    except AttestationError:
        raise
    except (ValueError, TypeError) as exc:
        raise AttestationError("malformed attestation document") from exc


def _verify(document: bytes, policy: Policy, nonce: bytes, now: datetime) -> AttestationDocument:
    try:
        sign1 = cose.decode(document)
        doc = AttestationDocument.decode(sign1.payload)
    except ValueError as exc:
        raise AttestationError("malformed attestation document") from exc

    leaf = _verify_chain(doc, policy.trust_anchor, now)
    public_key = leaf.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        raise AttestationError("signing certificate must hold an EC key")
    try:
        if sign1.alg != cose.ES384:
            raise AttestationError("attestation must be signed with ES384")
        sign1.verify(public_key)
    except cose.CoseError as exc:
        raise AttestationError("attestation signature does not verify") from exc

    if doc.digest != DIGEST:
        raise AttestationError("unsupported PCR digest")
    pcr0 = doc.pcrs.get(0, b"")
    if not hmac.compare_digest(pcr0, policy.expected_pcr0):
        raise AttestationError("PCR0 does not match the expected enclave image")
    if doc.nonce is None or not hmac.compare_digest(doc.nonce, nonce):
        raise AttestationError("nonce does not match: replayed or foreign document")
    issued = datetime.fromtimestamp(doc.timestamp_ms / 1000, UTC)
    if not now - policy.max_age <= issued <= now + policy.max_clock_skew:
        raise AttestationError("attestation timestamp is outside the accepted window")
    return doc


def _verify_chain(
    doc: AttestationDocument, anchor: x509.Certificate, now: datetime
) -> x509.Certificate:
    try:
        chain = [x509.load_der_x509_certificate(der) for der in (*doc.cabundle, doc.certificate)]
    except ValueError as exc:
        raise AttestationError("malformed certificate in attestation") from exc
    if chain[0] != anchor:
        raise AttestationError("certificate chain does not start at the trust anchor")
    for issuer, subject in pairwise(chain):
        try:
            subject.verify_directly_issued_by(issuer)
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise AttestationError("certificate chain is broken") from exc
    for cert in chain:
        if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
            raise AttestationError("certificate in chain is outside its validity period")
    for depth, issuer in enumerate(chain[:-1]):
        _require_issuer(issuer, cas_below=len(chain) - 2 - depth)
    leaf = chain[-1]
    leaf_constraints = _extension(leaf, x509.BasicConstraints)
    if leaf_constraints is not None and leaf_constraints.ca:
        raise AttestationError("signing certificate must not be a CA")
    return leaf


def _require_issuer(cert: x509.Certificate, cas_below: int) -> None:
    constraints = _extension(cert, x509.BasicConstraints)
    usage = _extension(cert, x509.KeyUsage)
    if constraints is None or not constraints.ca:
        raise AttestationError("issuer in chain is not a CA")
    if constraints.path_length is not None and cas_below > constraints.path_length:
        raise AttestationError("chain exceeds an issuer's path length")
    if usage is not None and not usage.key_cert_sign:
        raise AttestationError("issuer in chain may not sign certificates")


def _extension[E: x509.ExtensionType](cert: x509.Certificate, kind: type[E]) -> E | None:
    try:
        return cert.extensions.get_extension_for_class(kind).value
    except x509.ExtensionNotFound:
        return None


@dataclass(frozen=True)
class EnclaveIdentity:
    """What a verified attestation tells a relying party about this enclave instance."""

    encryption_key: x25519.X25519PublicKey
    signing_key: ed25519.Ed25519PublicKey
    tls_cert_sha256: bytes
    pcr0: bytes
    issued_at: datetime

    @property
    def key_id(self) -> bytes:
        return envelope.key_id(self.encryption_key)

    @classmethod
    def from_document(cls, doc: AttestationDocument) -> "EnclaveIdentity":
        if doc.public_key is None or doc.user_data is None:
            raise AttestationError("attestation does not carry enclave keys")
        try:
            user_data: dict[Any, Any] = cbor.decode_map(doc.user_data)
            signing_key = ed25519.Ed25519PublicKey.from_public_bytes(
                cbor.field(user_data, "sig", bytes)
            )
            tls = cbor.field(user_data, "tls", bytes)
            encryption_key = envelope.load_public_key(doc.public_key)
        except ValueError as exc:
            raise AttestationError("attestation carries malformed enclave keys") from exc
        return cls(
            encryption_key=encryption_key,
            signing_key=signing_key,
            tls_cert_sha256=tls,
            pcr0=doc.pcrs[0],
            issued_at=datetime.fromtimestamp(doc.timestamp_ms / 1000, UTC),
        )


def enclave_user_data(signing_key: ed25519.Ed25519PublicKey, tls_cert_der: bytes) -> bytes:
    raw = signing_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return cbor.encode({"sig": raw, "tls": hashlib.sha256(tls_cert_der).digest()})


def verify_enclave(
    document: bytes, policy: Policy, nonce: bytes, now: datetime | None = None
) -> EnclaveIdentity:
    return EnclaveIdentity.from_document(verify(document, policy, nonce, now))
