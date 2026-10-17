"""Issue the parent's mTLS identity: a CA that only signs the parent, and its client cert.

The CA key exists only in this process and is dropped once the leaf is signed, so nothing
on disk can mint another parent certificate. Renewal means re-running this script.
"""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from protocol.attestation import PARENT_TLS_NAME

CA_LIFETIME: Final = timedelta(days=30)
LEAF_LIFETIME: Final = timedelta(days=7)


def issue_parent_identity(
    now: datetime | None = None,
) -> tuple[x509.Certificate, x509.Certificate, ec.EllipticCurvePrivateKey]:
    now = now or datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("LegalDocs parent CA")
    ca = (
        _builder(ca_name, ca_name, ca_key.public_key(), now, CA_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_usage(cert_sign=True), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        _builder(_name(PARENT_TLS_NAME), ca_name, leaf_key.public_key(), now, LEAF_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_usage(cert_sign=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(PARENT_TLS_NAME)]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return ca, leaf, leaf_key


def write(directory: Path) -> None:
    ca, leaf, key = issue_parent_identity()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    (directory / "parent-ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "parent.crt").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path = directory / "parent.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # Containers run as another uid; the 0700 directory is what keeps the key private here.
    key_path.chmod(0o644)


def _builder(
    subject: x509.Name,
    issuer: x509.Name,
    public_key: ec.EllipticCurvePublicKey,
    now: datetime,
    lifetime: timedelta,
) -> x509.CertificateBuilder:
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + lifetime)
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _usage(*, cert_sign: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not cert_sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=cert_sign,
        crl_sign=cert_sign,
        encipher_only=False,
        decipher_only=False,
    )


def is_fresh(directory: Path, margin: timedelta = timedelta(days=1)) -> bool:
    paths = [directory / name for name in ("parent-ca.crt", "parent.crt", "parent.key")]
    if not all(path.exists() for path in paths):
        return False
    leaf = x509.load_pem_x509_certificate(paths[1].read_bytes())
    return leaf.not_valid_after_utc - datetime.now(UTC) > margin


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("certs"))
    parser.add_argument("--if-stale", action="store_true", help="keep certs valid for another day")
    args = parser.parse_args()
    if not (args.if_stale and is_fresh(args.directory)):
        write(args.directory)


if __name__ == "__main__":
    main()
