import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

import pytest
from redis.exceptions import LockError
from sqlalchemy import select

from client.sdk import ClientError, LegalDocsClient, ProcessingFailed
from enclave.extractor import extract_fields
from enclave.service import EnclaveKeys, EnclaveService
from nsm.device import MockNsm
from parent import pipeline
from parent.chain import Anchored, ChainError
from parent.enclave_link import EnclaveRejected, EnclaveUnavailable
from parent.models import Document
from parent.pipeline import Outcome
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
    stack.link.enclave = EnclaveService(nsm, EnclaveKeys.generate(), extract_fields)

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


class _BrokenLink:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def attestation(self, nonce: bytes) -> bytes:
        raise AssertionError("not used")

    def process(self, request: object) -> NoReturn:
        raise self.error


def _submit_unprocessed(stack: Stack, client: LegalDocsClient) -> UUID:
    stack.process_inline = False
    return client.submit(CONTRACT.encode()).document_id


def _status(stack: Stack, document_id: UUID) -> tuple[str, str | None]:
    with stack.sessions() as session:
        document = session.get(Document, ("acme", document_id))
        assert document is not None
        return document.status, document.error_code


@pytest.mark.parametrize(
    ("error", "last_attempt", "expected"),
    [
        (EnclaveUnavailable("down"), False, (Outcome.RETRY, "PROCESSING", None)),
        (EnclaveUnavailable("down"), True, (Outcome.FAILED, "FAILED", "enclave_unavailable")),
        (ValueError("bad enclave response"), False, (Outcome.FAILED, "FAILED", "internal_error")),
        (
            EnclaveRejected("processing_timeout", 422),
            False,
            (Outcome.FAILED, "FAILED", "processing_timeout"),
        ),
    ],
    ids=["unavailable, will retry", "unavailable, last attempt", "unexpected error", "timeout"],
)
def test_a_document_never_stays_processing_after_its_last_attempt(
    stack: Stack,
    acme: LegalDocsClient,
    error: Exception,
    last_attempt: bool,
    expected: tuple[Outcome, str, str | None],
) -> None:
    document_id = _submit_unprocessed(stack, acme)
    with stack.sessions() as session:
        outcome = pipeline.process_document(
            session, _BrokenLink(error), "acme", document_id, last_attempt=last_attempt
        )
    assert (outcome, *_status(stack, document_id)) == expected


def test_waiting_for_the_anchor_waits_for_the_mined_timestamp_not_the_tx_hash(
    stack: Stack, acme: LegalDocsClient
) -> None:
    submission = acme.submit(CONTRACT.encode())
    with stack.sessions() as session:
        document = session.get(Document, ("acme", submission.document_id))
        assert document is not None
        document.anchor_tx = "0x" + "ab" * 32
        session.commit()
    with pytest.raises(ClientError, match="not ready"):
        acme.wait(submission, anchored=True, timeout=0.3)

    with stack.sessions() as session:
        document = session.get(Document, ("acme", submission.document_id))
        assert document is not None
        document.anchored_at = datetime.now(UTC)
        session.commit()
    assert acme.wait(submission, anchored=True, timeout=1).anchored_at


class _FakeRelayer:
    def __init__(self, result: Anchored | Exception) -> None:
        self.result = result

    def anchor(
        self, receipt_hash: bytes, *, known_tx: str | None, on_sent: Callable[[str], None]
    ) -> Anchored:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    "error",
    [LockError("nonce lock busy"), ChainError("no registry yet"), ConnectionError("rpc down")],
    ids=["nonce lock timeout", "registry missing", "rpc unreachable"],
)
def test_anchoring_retries_when_the_lock_or_chain_is_unavailable(
    stack: Stack, acme: LegalDocsClient, error: Exception
) -> None:
    document_id = acme.submit(CONTRACT.encode()).document_id

    def relayer() -> _FakeRelayer:
        if isinstance(error, ChainError):
            raise error
        return _FakeRelayer(error)

    with stack.sessions() as session:
        assert pipeline.anchor_document(session, relayer, "acme", document_id) is Outcome.RETRY
        document = session.get(Document, ("acme", document_id))
        assert document is not None
        assert document.anchored_at is None


def test_an_anchor_found_without_its_tx_hash_never_stores_an_empty_one(
    stack: Stack, acme: LegalDocsClient
) -> None:
    document_id = acme.submit(CONTRACT.encode()).document_id
    with stack.sessions() as session:
        outcome = pipeline.anchor_document(
            session, lambda: _FakeRelayer(Anchored(None, 1_800_000_000)), "acme", document_id
        )
        document = session.get(Document, ("acme", document_id))
    assert outcome is Outcome.DONE
    assert document is not None
    assert document.anchor_tx is None
    assert document.anchored_at is not None
