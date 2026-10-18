"""What the worker does with a document. The parent only moves ciphertext and receipts.

Each step ends in one of three outcomes. A document never stays PROCESSING after its last
attempt: anything that is not a retry or a success leaves it FAILED with a code.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Final, Protocol
from uuid import UUID

from redis.exceptions import RedisError
from sqlalchemy.orm import Session
from web3.exceptions import Web3Exception

from parent.chain import Anchored, ChainError
from parent.enclave_link import EnclaveLink, EnclaveRejected, EnclaveUnavailable
from parent.models import Document, Status
from protocol import receipt
from protocol.envelope import Binding
from protocol.messages import ProcessRequest

KEY_ROTATED: Final = "enclave_key_rotated"
TIMED_OUT: Final = "processing_timeout"
REJECTED: Final = "rejected_by_enclave"
UNAVAILABLE: Final = "enclave_unavailable"
INTERNAL: Final = "internal_error"

_ENCLAVE_CODES: Final = {"stale_key": KEY_ROTATED, "processing_timeout": TIMED_OUT}
_RETRYABLE_ANCHOR_ERRORS: Final = (ChainError, Web3Exception, RedisError, OSError)

log = logging.getLogger("parent.pipeline")


class Outcome(Enum):
    DONE = "done"
    RETRY = "retry"
    FAILED = "failed"


class Anchorer(Protocol):
    def anchor(
        self, receipt_hash: bytes, *, known_tx: str | None, on_sent: Callable[[str], None]
    ) -> Anchored: ...


def process_document(
    session: Session, link: EnclaveLink, tenant_id: str, document_id: UUID, *, last_attempt: bool
) -> Outcome:
    try:
        return _process(session, link, tenant_id, document_id)
    except EnclaveUnavailable as exc:
        if not last_attempt:
            log.warning("document=%s will retry: %s", document_id, exc)
            return Outcome.RETRY
        _give_up(session, tenant_id, document_id, UNAVAILABLE)
    except Exception:
        log.exception("document=%s failed", document_id)
        _give_up(session, tenant_id, document_id, INTERNAL)
    return Outcome.FAILED


def anchor_document(
    session: Session, relayer: Callable[[], Anchorer], tenant_id: str, document_id: UUID
) -> Outcome:
    document = session.get(Document, (tenant_id, document_id))
    if document is None or document.receipt_hash is None or document.anchored_at is not None:
        return Outcome.DONE

    def remember(tx_hash: str) -> None:
        document.anchor_tx = tx_hash
        session.commit()

    try:
        anchored = relayer().anchor(
            document.receipt_hash, known_tx=document.anchor_tx, on_sent=remember
        )
    except _RETRYABLE_ANCHOR_ERRORS as exc:
        session.rollback()
        log.warning("anchoring document=%s will retry: %r", document_id, exc)
        return Outcome.RETRY
    if anchored.tx_hash:
        document.anchor_tx = anchored.tx_hash
    document.anchored_at = datetime.fromtimestamp(anchored.timestamp, UTC)
    session.commit()
    log.info("anchored document=%s tx=%s", document_id, document.anchor_tx)
    return Outcome.DONE


def _process(session: Session, link: EnclaveLink, tenant_id: str, document_id: UUID) -> Outcome:
    document = session.get(Document, (tenant_id, document_id))
    if document is None or document.status == Status.FAILED:
        return Outcome.FAILED
    if document.status == Status.COMPLETED:
        return Outcome.DONE
    document.status = Status.PROCESSING
    session.commit()

    request = ProcessRequest(Binding(tenant_id, document_id), document.envelope)
    try:
        response = link.process(request)
    except EnclaveRejected as exc:
        _fail(session, document, _ENCLAVE_CODES.get(exc.code, REJECTED))
        return Outcome.FAILED

    document.sealed_result = response.sealed_result
    document.receipt = response.receipt
    document.receipt_hash = receipt.anchor_hash(response.receipt)
    document.status = Status.COMPLETED
    session.commit()
    log.info("processed document=%s", document_id)
    return Outcome.DONE


def _give_up(session: Session, tenant_id: str, document_id: UUID, code: str) -> None:
    session.rollback()
    document = session.get(Document, (tenant_id, document_id))
    if document is not None and document.status != Status.COMPLETED:
        _fail(session, document, code)


def _fail(session: Session, document: Document, code: str) -> None:
    document.status = Status.FAILED
    document.error_code = code
    session.commit()
    log.warning("document=%s failed: %s", document.id, code)
