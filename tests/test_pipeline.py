import json
import logging

import pytest
from sqlalchemy import select

from client.sdk import ClientError, LegalDocsClient, ProcessingFailed
from enclave.service import EnclaveKeys, EnclaveService
from nsm.device import MockNsm
from parent.models import Document
from protocol import receipt
from protocol.envelope import EnvelopeError
from tests.conftest import Stack

MARKER = "Quillfeather Marker Holdings LLC"
CONTRACT = (
    f"This Agreement is made on March 3, 2026 by and between {MARKER}, a Delaware company, "
    "and Brightwater Ltd., a UK company."
)


@pytest.fixture
def acme(stack: Stack, nsm: MockNsm, pcrs: dict[int, bytes]) -> LegalDocsClient:
    return stack.client(stack.tenant("acme"), nsm, pcrs[0])


def test_the_client_gets_extracted_fields_and_a_matching_receipt(
    acme: LegalDocsClient, pcrs: dict[int, bytes]
) -> None:
    result = acme.process(CONTRACT.encode())
    assert MARKER in result.fields["parties"]
    assert "March 3, 2026" in result.fields["dates"]
    assert result.receipt.pcr0 == pcrs[0]
    assert result.receipt.input_hash != receipt.commit(bytes(32), CONTRACT.encode())


def test_the_parent_never_sees_plaintext(
    stack: Stack, acme: LegalDocsClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = acme.process(CONTRACT.encode())
    assert MARKER in result.fields["parties"]

    seen_by_parent = [
        stack.database.read_bytes(),
        *stack.link.traffic,
        json.dumps(acme.list_documents()).encode(),
        caplog.text.encode(),
    ]
    for needle in (MARKER.encode(), b"Brightwater", b"March 3, 2026"):
        assert not any(needle in blob for blob in seen_by_parent), needle


def test_results_open_only_with_the_reply_key_of_their_own_request(acme: LegalDocsClient) -> None:
    first = acme.submit(CONTRACT.encode())
    second = acme.submit(b"Second contract between Foo Inc. and Bar LLC.")
    second_document = acme.status(second.document_id)
    with pytest.raises(EnvelopeError):
        acme.open(first, second_document)


def test_a_parent_that_moves_a_document_to_another_tenant_gets_nothing(
    stack: Stack, acme: LegalDocsClient
) -> None:
    stack.process_inline = False
    submission = acme.submit(CONTRACT.encode())
    stack.tenant("globex")
    with stack.sessions() as session:
        original = session.get(Document, ("acme", submission.document_id))
        assert original is not None
        session.add(
            Document(tenant_id="globex", id=submission.document_id, envelope=original.envelope)
        )
        session.commit()

    stack.run("globex", submission.document_id)
    with stack.sessions() as session:
        moved = session.get(Document, ("globex", submission.document_id))
        assert moved is not None
        assert (moved.status, moved.error_code, moved.sealed_result) == (
            "FAILED",
            "rejected_by_enclave",
            None,
        )


def test_swapped_results_or_receipts_are_caught_by_the_client(
    stack: Stack, acme: LegalDocsClient
) -> None:
    first = acme.submit(CONTRACT.encode())
    second = acme.submit(b"Agreement between Foo Inc. and Bar LLC dated 2026-01-02.")
    with stack.sessions() as session:
        a = session.get(Document, ("acme", first.document_id))
        b = session.get(Document, ("acme", second.document_id))
        assert a is not None
        assert b is not None
        a.receipt, a.receipt_hash = b.receipt, b.receipt_hash
        session.commit()
    with pytest.raises(ClientError, match="receipt"):
        acme.wait(first)


def test_a_stale_enclave_key_fails_with_a_defined_code_and_the_client_recovers(
    stack: Stack, acme: LegalDocsClient, nsm: MockNsm
) -> None:
    before_restart = acme.attest()
    stack.link.enclave = EnclaveService(nsm, EnclaveKeys.generate())

    stale = acme.submit(CONTRACT.encode(), before_restart)
    with pytest.raises(ProcessingFailed) as failure:
        acme.wait(stale)
    assert failure.value.code == "enclave_key_rotated"

    assert MARKER in acme.process(CONTRACT.encode()).fields["parties"]


def test_the_stored_receipt_hash_is_what_gets_anchored(stack: Stack, acme: LegalDocsClient) -> None:
    result = acme.process(CONTRACT.encode())
    with stack.sessions() as session:
        stored = session.scalars(select(Document)).one()
    assert stored.receipt is not None
    assert stored.receipt_hash == receipt.anchor_hash(stored.receipt) == result.receipt_hash
