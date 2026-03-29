"""Celery task definitions for the LegalDocs processing pipeline.

Three-task chain: ingest_document -> forward_to_enclave -> store_results
"""

import base64
import logging
import os
import ssl
import time

import httpx
from celery import Task
from celery.exceptions import Retry
from sqlalchemy.orm import Session, sessionmaker

from parent.models.models import Document
from parent.db import get_engine
from parent.logging_config import setup_logging
from parent.workers.celery_app import celery_app

logger = setup_logging("worker")


def _get_db_session() -> Session:
    """Create a SQLAlchemy session for use in Celery tasks."""
    engine = get_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()


def _get_enclave_client() -> httpx.Client:
    """Create a synchronous mTLS httpx client for enclave communication."""
    enclave_host = os.getenv("ENCLAVE_HOST", "enclave")
    enclave_port = os.getenv("ENCLAVE_PORT", "8443")
    ca_cert = os.getenv("CA_CERT_PATH", "/certs/ca.crt")
    client_cert = os.getenv("CLIENT_CERT_PATH", "/certs/parent.crt")
    client_key = os.getenv("CLIENT_KEY_PATH", "/certs/parent.key")

    ssl_context = ssl.create_default_context()
    ssl_context.load_verify_locations(ca_cert)
    ssl_context.load_cert_chain(client_cert, client_key)

    return httpx.Client(
        base_url=f"https://{enclave_host}:{enclave_port}",
        verify=ssl_context,
        timeout=60.0,
    )


@celery_app.task(bind=True, name="parent.workers.tasks.ingest_document")
def ingest_document(self: Task, document_id: str) -> str:
    """Set document status to PROCESSING.

    First task in the chain. Marks the document as being processed.

    Args:
        document_id: UUID string of the document to process.

    Returns:
        The document_id string (passed to next task in chain).
    """
    start = time.monotonic()
    session = _get_db_session()
    try:
        doc = session.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            raise ValueError(f"Document {document_id} not found")

        doc.status = "PROCESSING"
        session.commit()
        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Document status set to PROCESSING",
            extra={
                "correlation_id": document_id,
                "tenant_id": doc.tenant_id,
                "stage": "worker.ingest",
                "duration_ms": round(duration_ms, 2),
            },
        )
        return document_id
    except Exception as exc:
        session.rollback()
        # Try to mark as FAILED
        try:
            doc = session.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "FAILED"
                doc.error_message = str(exc)
                session.commit()
        except Exception:
            session.rollback()
        logger.exception(
            "ingest_document failed",
            extra={"correlation_id": document_id, "stage": "worker.ingest"},
        )
        raise
    finally:
        session.close()


@celery_app.task(
    bind=True,
    name="parent.workers.tasks.forward_to_enclave",
    max_retries=3,
    default_retry_delay=5,
)
def forward_to_enclave(self: Task, document_id: str) -> dict:
    """Forward encrypted document content to the enclave for processing.

    Reads encrypted_content from DB, POSTs to enclave /process over mTLS,
    returns the encrypted result as base64.

    Args:
        document_id: UUID string of the document.

    Returns:
        Dict with document_id and encrypted_result_b64.
    """
    start = time.monotonic()
    session = _get_db_session()
    try:
        doc = session.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            raise ValueError(f"Document {document_id} not found")

        tenant_id = doc.tenant_id
        encrypted_content = doc.encrypted_content

        client = _get_enclave_client()
        try:
            response = client.post(
                "/process",
                content=encrypted_content,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Correlation-ID": document_id,
                },
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            logger.warning(
                "Connection error forwarding to enclave, retrying",
                extra={"correlation_id": document_id, "tenant_id": tenant_id, "stage": "worker.forward"},
            )
            raise self.retry(exc=exc, countdown=5, max_retries=3)
        finally:
            client.close()

        encrypted_result = response.content
        encrypted_result_b64 = base64.b64encode(encrypted_result).decode("utf-8")

        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Document forwarded to enclave successfully",
            extra={
                "correlation_id": document_id,
                "tenant_id": tenant_id,
                "stage": "worker.forward",
                "duration_ms": round(duration_ms, 2),
            },
        )
        return {
            "document_id": document_id,
            "encrypted_result_b64": encrypted_result_b64,
        }
    except Retry:
        # Re-raise retry exceptions without marking as FAILED
        raise
    except Exception as exc:
        session.rollback()
        try:
            doc = session.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "FAILED"
                doc.error_message = str(exc)
                session.commit()
        except Exception:
            session.rollback()
        logger.exception(
            "forward_to_enclave failed",
            extra={"correlation_id": document_id, "stage": "worker.forward"},
        )
        raise
    finally:
        session.close()


@celery_app.task(bind=True, name="parent.workers.tasks.store_results")
def store_results(self: Task, result: dict) -> str:
    """Store encrypted processing results in the database.

    Decodes the base64 encrypted result and stores it in the Document row,
    then sets status to COMPLETED.

    Args:
        result: Dict with 'document_id' and 'encrypted_result_b64' keys.

    Returns:
        The document_id string.
    """
    document_id = result["document_id"]
    encrypted_result_b64 = result["encrypted_result_b64"]

    start = time.monotonic()
    session = _get_db_session()
    try:
        doc = session.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            raise ValueError(f"Document {document_id} not found")

        tenant_id = doc.tenant_id
        doc.encrypted_result = base64.b64decode(encrypted_result_b64)
        doc.status = "COMPLETED"
        session.commit()
        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Document results stored, status COMPLETED",
            extra={
                "correlation_id": document_id,
                "tenant_id": tenant_id,
                "stage": "worker.store",
                "duration_ms": round(duration_ms, 2),
            },
        )
        return document_id
    except Exception as exc:
        session.rollback()
        try:
            doc = session.query(Document).filter(Document.id == document_id).first()
            if doc:
                doc.status = "FAILED"
                doc.error_message = str(exc)
                session.commit()
        except Exception:
            session.rollback()
        logger.exception(
            "store_results failed",
            extra={"correlation_id": document_id, "stage": "worker.store"},
        )
        raise
    finally:
        session.close()
