"""A software stand-in for the Nitro Security Module.

It holds the only key that can sign attestation documents and reports PCRs it measured
itself. In the compose stack it runs in its own container with no network; the enclave
reaches it through a Unix socket and the parent cannot reach it at all.
"""

import secrets
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from protocol import cose, nsm_api
from protocol.attestation import AttestationDocument

ROOT_LIFETIME: Final = timedelta(days=365)
SIGNER_LIFETIME: Final = timedelta(hours=3)
SIGNER_RENEW_BEFORE: Final = timedelta(minutes=15)


class MockNsm:
    def __init__(self, pcrs: Mapping[int, bytes], module_id: str | None = None) -> None:
        self.module_id = module_id or f"i-mock-enc{secrets.token_hex(8)}"
        self._pcrs = dict(pcrs)
        self._root_key = ec.generate_private_key(ec.SECP384R1())
        self.root_certificate = _root_certificate(self._root_key)
        self._lock = threading.Lock()
        self._signer: tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None = None

    @property
    def root_pem(self) -> bytes:
        return self.root_certificate.public_bytes(serialization.Encoding.PEM)

    def attest(self, *, public_key: bytes, user_data: bytes, nonce: bytes) -> bytes:
        if (
            len(public_key) > nsm_api.MAX_PUBLIC_KEY_BYTES
            or len(user_data) > nsm_api.MAX_USER_DATA_BYTES
            or len(nonce) > nsm_api.MAX_NONCE_BYTES
        ):
            raise ValueError("attestation input too large")
        key, certificate = self._current_signer()
        document = AttestationDocument(
            module_id=self.module_id,
            timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
            pcrs=self._pcrs,
            certificate=certificate.public_bytes(serialization.Encoding.DER),
            cabundle=(self.root_certificate.public_bytes(serialization.Encoding.DER),),
            public_key=public_key,
            user_data=user_data,
            nonce=nonce,
        )
        return cose.sign(document.encode(), key)

    def describe_pcr(self, index: int) -> bytes:
        if index not in self._pcrs:
            raise IndexError(index)
        return self._pcrs[index]

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            if isinstance(args := request.get("Attestation"), dict):
                document = self.attest(
                    public_key=_bytes(args, "public_key"),
                    user_data=_bytes(args, "user_data"),
                    nonce=_bytes(args, "nonce"),
                )
                return {"Attestation": {"document": document}}
            if isinstance(args := request.get("DescribePCR"), dict):
                index = args.get("index")
                if not isinstance(index, int):
                    return {"Error": "InvalidArgument"}
                return {"DescribePCR": {"lock": True, "data": self.describe_pcr(index)}}
        except IndexError:
            return {"Error": "InvalidIndex"}
        except ValueError:
            return {"Error": "InvalidArgument"}
        return {"Error": "InvalidOperation"}

    def _current_signer(self) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
        with self._lock:
            now = datetime.now(UTC)
            if (
                self._signer is None
                or self._signer[1].not_valid_after_utc - now < SIGNER_RENEW_BEFORE
            ):
                key = ec.generate_private_key(ec.SECP384R1())
                self._signer = (key, self._issue_signer(key, now))
            return self._signer

    def _issue_signer(self, key: ec.EllipticCurvePrivateKey, now: datetime) -> x509.Certificate:
        return (
            x509.CertificateBuilder()
            .subject_name(_name(self.module_id))
            .issuer_name(self.root_certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + SIGNER_LIFETIME)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(_usage(digital_signature=True), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._root_key.public_key()),
                critical=False,
            )
            .sign(self._root_key, hashes.SHA384())
        )


def _root_certificate(key: ec.EllipticCurvePrivateKey) -> x509.Certificate:
    now = datetime.now(UTC)
    name = _name("LegalDocs mock NSM root")
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + ROOT_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_usage(key_cert_sign=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA384())
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _usage(*, digital_signature: bool = False, key_cert_sign: bool = False) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=digital_signature,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=key_cert_sign,
        crl_sign=key_cert_sign,
        encipher_only=False,
        decipher_only=False,
    )


def _bytes(args: dict[str, Any], name: str) -> bytes:
    value = args.get(name, b"")
    if value is None:
        return b""
    if not isinstance(value, bytes):
        raise ValueError(f"{name} must be bytes")
    return value
