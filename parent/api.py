"""The public API. It authenticates tenants and stores ciphertext; it cannot decrypt anything."""

import base64
import binascii
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from parent import auth, worker
from parent.config import Settings
from parent.db import session_factory
from parent.enclave_link import AttestedTlsLink, EnclaveLink, EnclaveRejected, EnclaveUnavailable
from parent.models import Document, Status
from protocol.envelope import Envelope, EnvelopeError


class SubmitRequest(BaseModel):
    document_id: UUID
    envelope: str = Field(description="base64 of the HPKE envelope sealed to the enclave")


class DocumentView(BaseModel):
    id: UUID
    status: Status
    error_code: str | None
    sealed_result: str | None
    receipt: str | None
    receipt_hash: str | None
    anchor_tx: str | None
    anchored_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, document: Document) -> "DocumentView":
        return cls(
            id=document.id,
            status=document.status,
            error_code=document.error_code,
            sealed_result=_b64(document.sealed_result),
            receipt=_b64(document.receipt),
            receipt_hash=document.receipt_hash.hex() if document.receipt_hash else None,
            anchor_tx=document.anchor_tx,
            anchored_at=document.anchored_at,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class DocumentList(BaseModel):
    documents: list[DocumentView]


class AttestationView(BaseModel):
    document: str


class TenantView(BaseModel):
    tenant_id: str


@dataclass(frozen=True)
class Services:
    sessions: sessionmaker[Session]
    enclave: EnclaveLink
    enqueue: Callable[[str, UUID], None]
    max_envelope_bytes: int


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="LegalDocs", version="0.2.0")
    bearer = HTTPBearer(auto_error=False)

    def session() -> Iterator[Session]:
        with services.sessions() as db:
            yield db

    Db = Annotated[Session, Depends(session)]

    def tenant(
        db: Db, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ) -> str:
        tenant_id = auth.authenticate(db, credentials.credentials) if credentials else None
        if tenant_id is None:
            raise HTTPException(401, "invalid API key", headers={"WWW-Authenticate": "Bearer"})
        return tenant_id

    TenantId = Annotated[str, Depends(tenant)]

    @app.middleware("http")
    async def limit_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in ("POST", "PUT", "PATCH"):
            length = request.headers.get("content-length", "")
            if not length.isdigit():
                return JSONResponse({"detail": "Content-Length required"}, status_code=411)
            if int(length) > 2 * services.max_envelope_bytes:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/tenant")
    def whoami(tenant_id: TenantId) -> TenantView:
        return TenantView(tenant_id=tenant_id)

    @app.get("/attestation")
    def attestation(
        _: TenantId, nonce: Annotated[str, Query(pattern="^[0-9a-f]{32,128}$")]
    ) -> AttestationView:
        try:
            document = services.enclave.attestation(bytes.fromhex(nonce))
        except (EnclaveUnavailable, EnclaveRejected) as exc:
            raise HTTPException(503, "enclave unavailable") from exc
        return AttestationView(document=base64.b64encode(document).decode())

    @app.post("/documents", status_code=202)
    def submit(request: SubmitRequest, tenant_id: TenantId, db: Db) -> DocumentView:
        try:
            envelope = base64.b64decode(request.envelope, validate=True)
            Envelope.decode(envelope)
        except (binascii.Error, EnvelopeError) as exc:
            raise HTTPException(422, "envelope is not a sealed document") from exc
        if len(envelope) > services.max_envelope_bytes:
            raise HTTPException(413, "envelope too large")

        document = Document(tenant_id=tenant_id, id=request.document_id, envelope=envelope)
        db.add(document)
        try:
            db.commit()
        except IntegrityError as exc:
            raise HTTPException(409, "document id already used") from exc
        services.enqueue(tenant_id, document.id)
        db.refresh(document)
        return DocumentView.of(document)

    @app.get("/documents")
    def list_documents(tenant_id: TenantId, db: Db, status: Status | None = None) -> DocumentList:
        query = select(Document).where(Document.tenant_id == tenant_id)
        if status is not None:
            query = query.where(Document.status == status)
        documents = db.scalars(query.order_by(Document.created_at)).all()
        return DocumentList(documents=[DocumentView.of(d) for d in documents])

    @app.get("/documents/{document_id}")
    def get_document(document_id: UUID, tenant_id: TenantId, db: Db) -> DocumentView:
        document = db.get(Document, (tenant_id, document_id))
        if document is None:
            raise HTTPException(404, "document not found")
        return DocumentView.of(document)

    return app


def app_from_env() -> FastAPI:
    settings = Settings.from_env()
    link = AttestedTlsLink(
        settings.enclave_host,
        settings.enclave_port,
        client_cert=settings.parent_cert,
        client_key=settings.parent_key,
        trust_anchor=settings.nsm_trust_anchor,
        expected_pcr0=settings.expected_pcr0,
    )
    return create_app(
        Services(
            sessions=session_factory(settings.database_url),
            enclave=link,
            enqueue=worker.enqueue,
            max_envelope_bytes=settings.max_envelope_bytes,
        )
    )


def _b64(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data is not None else None
