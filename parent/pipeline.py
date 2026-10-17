"""What the worker does with a document. The parent only moves ciphertext and receipts."""

import logging
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from parent.chain import Relayer
from parent.enclave_link import EnclaveLink, EnclaveRejected
from parent.models import Document, Status
from protocol import receipt
from protocol.envelope import Binding
from protocol.messages import ProcessRequest

KEY_ROTATED: Final = "enclave_key_rotated"
REJECTED: Final = "rejected_by_enclave"
UNAVAILABLE: Final = "enclave_unavailable"

log = logging.getLogger("parent.pipeline")


def process_document(
    session: Session, link: EnclaveLink, tenant_id: str, document_id: UUID
) -> bool:
    document = session.get(Document, (tenant_id, document_id))
    if document is None or document.status in (Status.COMPLETED, Status.FAILED):
        return False
    document.status = Status.PROCESSING
    session.commit()

    request = ProcessRequest(Binding(tenant_id, document_id), document.envelope)
    try:
        response = link.process(request)
    except EnclaveRejected as exc:
        _fail(session, document, KEY_ROTATED if exc.code == "stale_key" else REJECTED)
        return False

    document.sealed_result = response.sealed_result
    document.receipt = response.receipt
    document.receipt_hash = receipt.anchor_hash(response.receipt)
    document.status = Status.COMPLETED
    session.commit()
    log.info("processed document=%s", document_id)
    return True


def give_up(session: Session, tenant_id: str, document_id: UUID) -> None:
    document = session.get(Document, (tenant_id, document_id))
    if document is not None and document.status != Status.COMPLETED:
        _fail(session, document, UNAVAILABLE)


def anchor_document(session: Session, relayer: Relayer, tenant_id: str, document_id: UUID) -> None:
    document = session.get(Document, (tenant_id, document_id))
    if document is None or document.receipt_hash is None or document.anchored_at is not None:
        return

    def remember(tx_hash: str) -> None:
        document.anchor_tx = tx_hash
        session.commit()

    anchored = relayer.anchor(document.receipt_hash, known_tx=document.anchor_tx, on_sent=remember)
    document.anchor_tx = anchored.tx_hash
    document.anchored_at = datetime.fromtimestamp(anchored.timestamp, UTC)
    session.commit()
    log.info("anchored document=%s tx=%s", document_id, anchored.tx_hash)


def _fail(session: Session, document: Document, code: str) -> None:
    document.status = Status.FAILED
    document.error_code = code
    session.commit()
    log.warning("document=%s failed: %s", document.id, code)
