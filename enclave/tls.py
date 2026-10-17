"""The enclave's TLS identity: a key made at boot and a self-signed certificate.

Nobody issues this certificate. The parent trusts it only after an attestation document
binds its hash to the enclave's measured code (attested TLS).
"""

import secrets
import ssl
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from protocol.attestation import ENCLAVE_TLS_NAME, PARENT_TLS_NAME

LIFETIME: Final = timedelta(hours=24)


def self_signed_certificate(
    key: ec.EllipticCurvePrivateKey, hostname: str = ENCLAVE_TLS_NAME
) -> x509.Certificate:
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + LIFETIME)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )


def server_context(
    key: ec.EllipticCurvePrivateKey, certificate: x509.Certificate, parent_ca_pem: bytes
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=parent_ca_pem.decode())
    load_identity(context, key, certificate)
    return context


# ssl loads keys only from files; the file holds the key encrypted under a password that
# exists only in memory, and lives on the container's tmpfs for the length of this call.
def load_identity(
    context: ssl.SSLContext, key: ec.EllipticCurvePrivateKey, certificate: x509.Certificate
) -> None:
    password = secrets.token_bytes(32)
    with tempfile.TemporaryDirectory() as directory:
        cert_path, key_path = Path(directory, "cert.pem"), Path(directory, "key.pem")
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.BestAvailableEncryption(password),
            )
        )
        context.load_cert_chain(cert_path, key_path, password=password)


def is_parent(peer: Mapping[str, object] | None) -> bool:
    names = peer.get("subjectAltName", ()) if peer else ()
    return isinstance(names, tuple) and ("DNS", PARENT_TLS_NAME) in names
