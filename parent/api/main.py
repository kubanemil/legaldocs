from __future__ import annotations

import base64
import os
import ssl
import time
from typing import Optional

import httpx
from celery import chain
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from parent.api.dependencies import get_db, get_tenant_id
from parent.api.schemas import (
    AttestationResponse,
    DecryptRequest,
    DecryptResponse,
    DocumentListResponse,
    DocumentStatusResponse,
    DocumentSubmitRequest,
    DocumentSubmitResponse,
    HealthResponse,
)
from parent.logging_config import setup_logging
from parent.models.models import Document
from parent.workers.tasks import forward_to_enclave, ingest_document, store_results

logger = setup_logging("api")

app = FastAPI(title="LegalDocs API")

# Enclave connection settings (from environment)
ENCLAVE_HOST = os.getenv("ENCLAVE_HOST", "enclave")
ENCLAVE_PORT = os.getenv("ENCLAVE_PORT", "8443")
CA_CERT_PATH = os.getenv("CA_CERT_PATH", "/certs/ca.crt")
CLIENT_CERT_PATH = os.getenv("CLIENT_CERT_PATH", "/certs/parent.crt")
CLIENT_KEY_PATH = os.getenv("CLIENT_KEY_PATH", "/certs/parent.key")


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="healthy", service="api")


@app.get("/attestation", response_model=AttestationResponse)
async def get_attestation() -> AttestationResponse:
    """Proxy attestation request to the enclave over mTLS."""
    enclave_url = f"https://{ENCLAVE_HOST}:{ENCLAVE_PORT}/attestation"
    start = time.monotonic()

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.load_verify_locations(CA_CERT_PATH)
        ssl_context.load_cert_chain(CLIENT_CERT_PATH, CLIENT_KEY_PATH)
        async with httpx.AsyncClient(verify=ssl_context) as client:
            response = await client.get(enclave_url, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            duration_ms = (time.monotonic() - start) * 1000
            logger.info(
                "Attestation retrieved",
                extra={"stage": "api.attestation", "duration_ms": round(duration_ms, 2), "status_code": 200},
            )
            return AttestationResponse(**data)
    except httpx.ConnectError as exc:
        logger.error("Attestation failed: cannot reach enclave", extra={"stage": "api.attestation", "status_code": 502})
        raise HTTPException(
            status_code=502, detail=f"Cannot reach enclave: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Attestation failed: enclave error", extra={"stage": "api.attestation", "status_code": 502})
        raise HTTPException(
            status_code=502,
            detail=f"Enclave returned error: {exc.response.status_code}",
        ) from exc
    except Exception as exc:
        logger.error("Attestation proxy error", extra={"stage": "api.attestation", "status_code": 502})
        raise HTTPException(
            status_code=502, detail=f"Attestation proxy error: {exc}"
        ) from exc


@app.post("/documents", response_model=DocumentSubmitResponse)
async def submit_document(
    request: DocumentSubmitRequest,
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> DocumentSubmitResponse:
    """Submit an encrypted document for processing.

    Decodes the base64-encoded ciphertext, creates a Document row with
    PENDING status, and dispatches the Celery processing chain.
    Tenant is identified by the X-Tenant-ID header.
    """
    try:
        encrypted_bytes = base64.b64decode(request.encrypted_content)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid base64 in encrypted_content: {exc}",
        ) from exc

    start = time.monotonic()

    doc = Document(
        tenant_id=tenant_id,
        encrypted_content=encrypted_bytes,
        filename=request.filename,
        status="PENDING",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    document_id = str(doc.id)

    # Dispatch Celery chain: ingest -> forward -> store
    pipeline = chain(
        ingest_document.s(document_id),
        forward_to_enclave.s(),
        store_results.s(),
    )
    pipeline.apply_async()

    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "Document submitted",
        extra={
            "correlation_id": document_id,
            "tenant_id": tenant_id,
            "stage": "api.submit",
            "duration_ms": round(duration_ms, 2),
            "status_code": 200,
        },
    )

    return DocumentSubmitResponse(id=document_id, status="PENDING")


@app.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    tenant_id: str = Depends(get_tenant_id),
    status: Optional[str] = None,
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """List all documents for the authenticated tenant.

    Optionally filter by status query parameter.
    """
    query = db.query(Document).filter(Document.tenant_id == tenant_id)
    if status is not None:
        query = query.filter(Document.status == status)
    docs = query.all()

    documents = []
    for doc in docs:
        encrypted_result_b64 = None
        if doc.encrypted_result is not None:
            encrypted_result_b64 = base64.b64encode(doc.encrypted_result).decode(
                "utf-8"
            )
        documents.append(
            DocumentStatusResponse(
                id=str(doc.id),
                status=doc.status,
                encrypted_result=encrypted_result_b64,
                error_message=doc.error_message,
                created_at=doc.created_at.isoformat(),
                updated_at=doc.updated_at.isoformat(),
            )
        )

    return DocumentListResponse(documents=documents)


@app.get("/documents/{document_id}", response_model=DocumentStatusResponse)
async def get_document_status(
    document_id: str,
    tenant_id: str = Depends(get_tenant_id),
    db: Session = Depends(get_db),
) -> DocumentStatusResponse:
    """Retrieve document processing status and results, scoped by tenant."""
    start = time.monotonic()
    doc = (
        db.query(Document)
        .filter(Document.id == document_id, Document.tenant_id == tenant_id)
        .first()
    )
    if doc is None:
        logger.warning(
            "Document not found",
            extra={"correlation_id": document_id, "tenant_id": tenant_id, "stage": "api.get_status", "status_code": 404},
        )
        raise HTTPException(status_code=404, detail="Document not found")

    encrypted_result_b64 = None
    if doc.encrypted_result is not None:
        encrypted_result_b64 = base64.b64encode(doc.encrypted_result).decode("utf-8")

    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "Document status retrieved",
        extra={
            "correlation_id": document_id,
            "tenant_id": tenant_id,
            "stage": "api.get_status",
            "duration_ms": round(duration_ms, 2),
            "status_code": 200,
        },
    )

    return DocumentStatusResponse(
        id=str(doc.id),
        status=doc.status,
        encrypted_result=encrypted_result_b64,
        error_message=doc.error_message,
        created_at=doc.created_at.isoformat(),
        updated_at=doc.updated_at.isoformat(),
    )


@app.post("/decrypt", response_model=DecryptResponse)
async def decrypt_result(request: DecryptRequest) -> DecryptResponse:
    """Proxy decryption request to the enclave over mTLS.

    Accepts base64-encoded encrypted data, forwards raw bytes to the
    enclave /decrypt endpoint, and returns the decrypted result as base64.
    """
    try:
        encrypted_bytes = base64.b64decode(request.encrypted_data)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid base64 in encrypted_data: {exc}",
        ) from exc

    enclave_url = f"https://{ENCLAVE_HOST}:{ENCLAVE_PORT}/decrypt"
    start = time.monotonic()

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.load_verify_locations(CA_CERT_PATH)
        ssl_context.load_cert_chain(CLIENT_CERT_PATH, CLIENT_KEY_PATH)
        async with httpx.AsyncClient(verify=ssl_context) as client:
            response = await client.post(
                enclave_url,
                content=encrypted_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=30.0,
            )
            response.raise_for_status()
    except httpx.ConnectError as exc:
        logger.error("Decrypt failed: cannot reach enclave", extra={"stage": "api.decrypt", "status_code": 502})
        raise HTTPException(
            status_code=502, detail=f"Cannot reach enclave: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.error("Decrypt failed: enclave error", extra={"stage": "api.decrypt", "status_code": 502})
        raise HTTPException(
            status_code=502,
            detail=f"Enclave returned error: {exc.response.status_code}",
        ) from exc
    except Exception as exc:
        logger.error("Decrypt proxy error", extra={"stage": "api.decrypt", "status_code": 502})
        raise HTTPException(
            status_code=502, detail=f"Decrypt proxy error: {exc}"
        ) from exc

    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "Decrypt result proxied",
        extra={"stage": "api.decrypt", "duration_ms": round(duration_ms, 2), "status_code": 200},
    )

    decrypted_b64 = base64.b64encode(response.content).decode("utf-8")
    return DecryptResponse(decrypted_data=decrypted_b64)
