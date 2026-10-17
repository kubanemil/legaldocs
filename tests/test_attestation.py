import os
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization

from enclave.service import EnclaveError, EnclaveService
from nsm.device import MockNsm
from protocol import cbor, cose
from protocol.attestation import (
    AttestationDocument,
    AttestationError,
    Policy,
    verify,
    verify_enclave,
)
from scripts.certs import issue_parent_identity


def attest(enclave: EnclaveService) -> tuple[bytes, bytes]:
    nonce = os.urandom(32)
    return enclave.attestation(nonce), nonce


def test_a_fresh_attestation_with_the_expected_pcr0_yields_the_enclave_keys(
    enclave: EnclaveService, policy: Policy
) -> None:
    document, nonce = attest(enclave)
    identity = verify_enclave(document, policy, nonce)
    assert identity.key_id == enclave.key_id
    assert identity.pcr0 == policy.expected_pcr0


def test_a_replayed_attestation_is_rejected(enclave: EnclaveService, policy: Policy) -> None:
    document, _ = attest(enclave)
    with pytest.raises(AttestationError, match="nonce"):
        verify(document, policy, os.urandom(32))


def test_a_wrong_pcr0_is_rejected(enclave: EnclaveService, policy: Policy) -> None:
    document, nonce = attest(enclave)
    other_image = Policy(policy.trust_anchor, bytes(48))
    with pytest.raises(AttestationError, match="PCR0"):
        verify(document, other_image, nonce)


@pytest.mark.parametrize(
    "offset", [timedelta(minutes=6), timedelta(minutes=-1)], ids=["old", "future"]
)
def test_an_attestation_outside_the_time_window_is_rejected(
    enclave: EnclaveService, policy: Policy, offset: timedelta
) -> None:
    document, nonce = attest(enclave)
    with pytest.raises(AttestationError, match="timestamp"):
        verify(document, policy, nonce, now=datetime.now(UTC) + offset)


def test_an_expired_nsm_signing_certificate_is_rejected(
    enclave: EnclaveService, policy: Policy
) -> None:
    document, nonce = attest(enclave)
    with pytest.raises(AttestationError, match="validity"):
        verify(document, policy, nonce, now=datetime.now(UTC) + timedelta(hours=4))


def test_a_document_from_an_nsm_the_client_does_not_trust_is_rejected(
    pcrs: dict[int, bytes], policy: Policy
) -> None:
    forger = MockNsm(pcrs)
    nonce = os.urandom(32)
    document = forger.attest(public_key=bytes(32), user_data=b"", nonce=nonce)
    with pytest.raises(AttestationError, match="trust anchor"):
        verify(document, policy, nonce)


def test_a_forged_signer_cannot_borrow_the_trusted_root(
    nsm: MockNsm, pcrs: dict[int, bytes], policy: Policy
) -> None:
    forger = MockNsm(pcrs)
    nonce = os.urandom(32)
    forged = cose.decode(forger.attest(public_key=bytes(32), user_data=b"", nonce=nonce))
    payload = AttestationDocument.decode(forged.payload)
    trusted_root = nsm.root_certificate.public_bytes(serialization.Encoding.DER)
    relabelled = AttestationDocument(**{**payload.__dict__, "cabundle": (trusted_root,)})
    document = cbor.encode([forged.protected, {}, relabelled.encode(), forged.signature])
    with pytest.raises(AttestationError, match="chain is broken"):
        verify(document, policy, nonce)


def test_the_parent_ca_cannot_stand_in_for_the_nsm(enclave: EnclaveService, policy: Policy) -> None:
    parent_ca, parent_cert, _ = issue_parent_identity()
    document, nonce = attest(enclave)
    signed = cose.decode(document)
    payload = AttestationDocument.decode(signed.payload)
    swapped = AttestationDocument(
        **{
            **payload.__dict__,
            "certificate": parent_cert.public_bytes(serialization.Encoding.DER),
            "cabundle": (parent_ca.public_bytes(serialization.Encoding.DER),),
        }
    )
    document = cbor.encode([signed.protected, {}, swapped.encode(), signed.signature])
    with pytest.raises(AttestationError, match="trust anchor"):
        verify(document, policy, nonce)


def test_editing_a_signed_document_breaks_its_signature(
    enclave: EnclaveService, policy: Policy
) -> None:
    document, nonce = attest(enclave)
    signed = cose.decode(document)
    payload = AttestationDocument.decode(signed.payload)
    edited = AttestationDocument(**{**payload.__dict__, "public_key": bytes(32)})
    tampered = cbor.encode([signed.protected, {}, edited.encode(), signed.signature])
    with pytest.raises(AttestationError, match="signature"):
        verify(tampered, policy, nonce)


@pytest.mark.parametrize(
    "nonce", [b"", os.urandom(8), os.urandom(65)], ids=["empty", "short", "long"]
)
def test_the_enclave_refuses_weak_or_oversized_nonces(
    enclave: EnclaveService, nonce: bytes
) -> None:
    with pytest.raises(EnclaveError):
        enclave.attestation(nonce)


def test_garbage_is_not_an_attestation(policy: Policy) -> None:
    with pytest.raises(AttestationError, match="malformed"):
        verify(b"\xa0", policy, os.urandom(32))
