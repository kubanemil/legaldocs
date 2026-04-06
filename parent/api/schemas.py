from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str


class AttestationPayload(BaseModel):
    enclave_id: str
    public_key: str
    timestamp: str
    platform: str


class AttestationResponse(BaseModel):
    payload: AttestationPayload
    signature: str
    certificate: str


class DocumentSubmitRequest(BaseModel):
    encrypted_content: str  # base64-encoded
    filename: Optional[str] = None


class DocumentSubmitResponse(BaseModel):
    id: str
    status: str


class DocumentStatusResponse(BaseModel):
    id: str
    status: str
    encrypted_result: Optional[str] = None  # base64-encoded
    content_hash: Optional[str] = None
    result_hash: Optional[str] = None
    tx_hash: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentStatusResponse]


class DecryptRequest(BaseModel):
    encrypted_data: str  # base64-encoded


class DecryptResponse(BaseModel):
    decrypted_data: str  # base64-encoded plaintext result
