---
date: 2026-03-29
author: claude-opus-4-6
status: draft
branch: n/a (no git repo)
related_research: docs/dev/legaldocs/m2-processing-pipeline/research.md
---

# Plan: M2 Processing Pipeline

## Overview

This plan implements the end-to-end document processing pipeline for M2. A client submits an encrypted legal document via `POST /documents`, a Celery chain orchestrates forwarding the ciphertext to the enclave over mTLS, the enclave decrypts, extracts parties and dates via regex, encrypts the results, and returns them. The worker stores encrypted results in PostgreSQL and the client retrieves them via `GET /documents/{id}`, then decrypts through a proxy endpoint. The parent API, database, and task queue never see plaintext document content.

## Architecture Decisions

- **Single `/process` endpoint on enclave**: Combines decrypt, extract, and re-encrypt into one atomic call. Simpler than separate endpoints, reduces round-trips, matches the press release description.
- **Encrypt results with enclave's own public key**: The enclave encrypts extraction results with its RSA public key. Client retrieves ciphertext and decrypts via a proxy `POST /decrypt` endpoint that forwards to the enclave. The parent sees decrypted extraction metadata (parties/dates) in transit but never the original document. Acceptable for MVP per research findings.
- **Three-task Celery chain**: `ingest_document` -> `forward_to_enclave` -> `store_results`. Each stage is independently retryable and monitorable. Matches production SQS-backed Celery pattern.
- **Single encrypted blob for results**: Store all extraction results as one encrypted blob in `Document.encrypted_result` rather than populating `ExtractedField` table per-field. Simpler for MVP; `ExtractedField` can be used in future milestones.
- **Synchronous httpx for worker**: Celery tasks are synchronous. Use `httpx.Client` (sync mode) for mTLS calls to enclave, consistent with the async `httpx.AsyncClient` already used in the API.
- **Base64 encoding for JSON serialization**: Celery enforces JSON serialization. Binary encrypted blobs are base64-encoded when passed between tasks and in API request/response bodies.
- **Shared mTLS certs between API and worker**: Both containers are in the parent trust domain and mount the same cert volume. No separate worker certs needed.

## Prerequisites

- [ ] M0 complete: Docker Compose services running (postgres, redis, api, worker, enclave)
- [ ] M1 complete: Enclave key pair, mTLS channel, client encryption/decryption, attestation flow
- [ ] Certs generated and available in `./certs/`

---

## Phase 1: Enclave Extractor + `/process` Endpoint

### Goal

Implement regex extraction in the enclave and add a `/process` POST endpoint that accepts ciphertext, decrypts it, extracts parties and dates, encrypts the results, and returns the encrypted blob.

### Files to Modify

- `enclave/extractor.py` - Replace stub with regex extraction logic
- `enclave/server.py` - Add `/process` POST route and handler

### Module Design

**`extractor.py`** - Regex-based extraction of parties and dates from legal document text.

Key functions:

- `extract_fields(text: str) -> dict` - Returns `{"parties": [str, ...], "dates": [str, ...]}`. Applies multiple regex patterns, deduplicates, strips whitespace.

Regex pattern categories:

- **Parties**: "BETWEEN X AND Y" clauses, "Party/Parties:" labels, "hereinafter" references, entity name patterns (Inc., LLC, Corp., Ltd.)
- **Dates**: ISO format (YYYY-MM-DD), US format (Month DD, YYYY), numeric (MM/DD/YYYY), ordinal ("15th day of January, 2024")

**`server.py` - `_handle_process` method** - New POST handler for `/process`.

Flow:

1. Read encrypted blob from request body
2. `self.server.crypto.decrypt_document(blob)` -> plaintext bytes
3. `extract_fields(plaintext.decode("utf-8"))` -> dict
4. Serialize dict to JSON bytes
5. `CryptoManager.encrypt_document(json_bytes, self.server.crypto.get_public_key_pem())` -> encrypted results
6. Return encrypted results as `application/octet-stream` (200)

Error handling:

- Decryption failure -> 400 with JSON `{"error": "..."}`
- Empty extraction -> 200 with encrypted empty results (valid outcome)

### Integration

- `do_POST` in `server.py` dispatches `/process` to `_handle_process`
- Imports `extract_fields` from `extractor` module
- Uses existing `CryptoManager` instance on `self.server.crypto`

### Success Criteria

**Manual Verification:**

- [ ] `curl -X POST https://enclave:8443/process --data-binary @encrypted_doc.bin` returns encrypted blob (via mTLS)
- [ ] Decrypting the returned blob yields JSON with `parties` and `dates` arrays
- [ ] Sending corrupt ciphertext returns 400 error
- [ ] Document with no extractable fields returns encrypted empty `{"parties": [], "dates": []}`

---

## Phase 2: Celery Tasks + API Endpoints

### Goal

Implement the three Celery chain tasks and the `POST /documents` / `GET /documents/{id}` API endpoints. This wires together the full server-side pipeline.

### Files to Modify

- `parent/workers/tasks.py` - Implement three pipeline tasks
- `parent/workers/celery_app.py` - Add task autodiscovery config
- `parent/api/main.py` - Add `POST /documents` and `GET /documents/{id}` endpoints, add `POST /decrypt` proxy endpoint
- `parent/api/schemas.py` - Add request/response Pydantic models
- `parent/api/dependencies.py` - No changes needed (already has `get_db`)

### Module Design

**`tasks.py`** - Three Celery chain tasks.

Key functions:

- `ingest_document(document_id: str) -> str` - Sets document status to PROCESSING. Returns `document_id`.
- `forward_to_enclave(document_id: str) -> dict` - Reads `encrypted_content` from DB, POSTs to enclave `/process` over mTLS, returns `{"document_id": str, "encrypted_result_b64": str}`.
- `store_results(result: dict) -> str` - Stores base64-decoded encrypted result in `Document.encrypted_result`, sets status to COMPLETED. Returns `document_id`.

Shared helper:

- `_get_enclave_client() -> httpx.Client` - Creates sync mTLS httpx client using env vars (same cert paths as API).
- `_get_db_session() -> Session` - Creates a SQLAlchemy session for use in tasks (not FastAPI dependency injection).

Error handling:

- Each task wraps in try/except; on failure, sets document status to FAILED with error message
- `forward_to_enclave` uses `self.retry(max_retries=3, countdown=5)` for connection errors
- Binary data base64-encoded for JSON serialization between tasks

**`celery_app.py`** - Add task include.

- Add `celery_app.conf.include = ["parent.workers.tasks"]` for autodiscovery.

**`schemas.py`** - New Pydantic models.

```
DocumentSubmitRequest {
    tenant_id: str
    encrypted_content: str  # base64-encoded
    filename: str | None
}

DocumentSubmitResponse {
    id: str
    status: str
}

DocumentStatusResponse {
    id: str
    status: str
    encrypted_result: str | None  # base64-encoded
    error_message: str | None
    created_at: str
    updated_at: str
}

DecryptRequest {
    encrypted_data: str  # base64-encoded
}

DecryptResponse {
    decrypted_data: str  # base64-encoded plaintext result
}
```

**`main.py`** - Three new endpoints.

- `POST /documents` - Validates request, decodes base64 to bytes, creates `Document` row (PENDING), dispatches Celery chain `ingest_document | forward_to_enclave | store_results`, returns `{id, status}`.
- `GET /documents/{document_id}` - Fetches `Document` from DB, returns status and base64-encoded encrypted result if COMPLETED.
- `POST /decrypt` - Accepts base64-encoded encrypted blob, forwards raw bytes to enclave `/decrypt` over mTLS, returns base64-encoded decrypted result. This is the proxy endpoint for client-side result decryption.

### Data Flow

```
POST /documents (ciphertext_b64)
  -> decode b64 -> store in Document.encrypted_content -> status=PENDING
  -> dispatch chain(ingest_document.s(doc_id), forward_to_enclave.s(), store_results.s())

ingest_document(doc_id)
  -> Document.status = PROCESSING -> return doc_id

forward_to_enclave(doc_id)
  -> read Document.encrypted_content -> POST to enclave:8443/process
  -> receive encrypted_result -> return {doc_id, encrypted_result_b64}

store_results({doc_id, encrypted_result_b64})
  -> decode b64 -> Document.encrypted_result = bytes -> status = COMPLETED

GET /documents/{id}
  -> return {id, status, encrypted_result_b64, error_message, timestamps}

POST /decrypt (encrypted_data_b64)
  -> decode b64 -> POST to enclave:8443/decrypt -> return decrypted_b64
```

### Integration

- API imports Celery chain from `parent.workers.tasks`
- Tasks import `get_engine` from `parent.db` for DB sessions
- Tasks use same env vars as API for enclave connection (ENCLAVE_HOST, ENCLAVE_PORT, cert paths)
- FastAPI `Depends(get_db)` for API endpoints

### Success Criteria

**Manual Verification:**

- [ ] `POST /documents` with base64-encoded ciphertext returns `{"id": "...", "status": "PENDING"}`
- [ ] Celery worker logs show task chain execution: ingest -> forward -> store
- [ ] `GET /documents/{id}` shows status progression: PENDING -> PROCESSING -> COMPLETED
- [ ] COMPLETED document has non-null `encrypted_result` in response
- [ ] `POST /decrypt` with the encrypted_result returns decryptable JSON with parties and dates
- [ ] Invalid ciphertext submission results in status=FAILED with error message

---

## Phase 3: Client SDK Updates

### Goal

Extend the client SDK with methods to submit documents, check status, and retrieve/decrypt results, completing the client-side flow.

### Files to Modify

- `client/client.py` - Add `submit_document`, `get_document_status`, `get_document_results` methods

### Module Design

**`client.py`** - New methods on `LegalDocsClient`.

Key functions:

- `submit_document(ciphertext: bytes, tenant_id: str, filename: str | None = None) -> str` - Base64-encodes ciphertext, POSTs to `/documents`, returns document ID string.
- `get_document_status(document_id: str) -> dict` - GETs `/documents/{document_id}`, returns full response dict with status, encrypted_result, etc.
- `get_document_results(document_id: str) -> dict` - Polls `get_document_status` until COMPLETED or FAILED (with timeout/max_retries), then calls `POST /decrypt` with encrypted_result, returns parsed JSON dict with `parties` and `dates`.

Helper:

- `decrypt_results(encrypted_result_b64: str) -> dict` - POSTs to `/decrypt` endpoint, decodes base64 response, parses JSON, returns extraction dict.

### Integration

- Uses existing `self.api_url` and `requests` library
- `submit_document` sends JSON body matching `DocumentSubmitRequest` schema
- `get_document_results` combines polling + decryption into a convenience method
- No direct enclave access needed; all through parent API proxy

### Success Criteria

**Manual Verification:**

- [ ] `client.submit_document(ciphertext, "tenant1")` returns a UUID string
- [ ] `client.get_document_status(doc_id)` returns dict with `status` field
- [ ] `client.get_document_results(doc_id)` returns `{"parties": [...], "dates": [...]}`
- [ ] Full client flow: encrypt -> submit -> poll -> get results works end-to-end

---

## Phase 4: Integration Verification

### Goal

Verify the full end-to-end pipeline works: client encrypts a sample legal document, submits it, the pipeline processes it through the enclave, and the client retrieves and decrypts the extraction results.

### Files to Modify

- No new production code. This phase verifies existing work from phases 1-3.

### E2E Test Scenarios

Derived from press release user stories:

1. **Happy path (M2.1-M2.5)**: Client encrypts a sample legal document containing known parties and dates -> submits via `POST /documents` -> polls until COMPLETED -> retrieves encrypted results -> decrypts via `/decrypt` proxy -> verifies extracted parties and dates match expected values.

2. **Status transitions**: Verify document status transitions PENDING -> PROCESSING -> COMPLETED in order.

3. **Failed processing**: Submit corrupt ciphertext (random bytes) -> verify status becomes FAILED with error message.

4. **Empty extraction**: Submit encrypted document with no parties or dates -> verify COMPLETED with empty `{"parties": [], "dates": []}`.

5. **Privacy verification**: After processing, query PostgreSQL directly and verify `encrypted_content` and `encrypted_result` columns contain only binary blobs (not plaintext). Verify Redis contains no document content.

### Sample Legal Document

```
AGREEMENT

This Agreement is made and entered into as of January 15, 2024,
by and between Acme Corporation, a Delaware corporation
("Company"), and John Smith ("Contractor").

WHEREAS, the Company desires to engage the Contractor...

Effective Date: 2024-01-15
Termination Date: December 31, 2024
```

Expected extraction:
- Parties: ["Acme Corporation", "John Smith"]
- Dates: ["January 15, 2024", "2024-01-15", "December 31, 2024"]

### Success Criteria

**Manual Verification:**

- [ ] Full E2E flow completes without errors
- [ ] Extracted parties include at least "Acme Corporation" from sample document
- [ ] Extracted dates include at least "2024-01-15" from sample document
- [ ] PostgreSQL `documents` table contains only encrypted blobs, zero plaintext
- [ ] Redis contains only task metadata (document IDs), no document content
- [ ] Document status correctly transitions through PENDING -> PROCESSING -> COMPLETED
- [ ] Failed document (corrupt ciphertext) transitions to FAILED with error message
- [ ] Enclave container has no internet access (verified by network config)

---

## Rollback Plan

Each phase modifies independent files. To rollback:

- **Phase 1**: Revert `enclave/extractor.py` to stub, remove `/process` handler from `enclave/server.py`
- **Phase 2**: Revert `parent/workers/tasks.py` to stub, remove new endpoints from `parent/api/main.py`, remove new schemas from `schemas.py`, remove `include` from `celery_app.py`
- **Phase 3**: Remove new methods from `client/client.py`

No database migrations are needed (schema already exists from M0).

## Open Questions

1. **Polling interval for client SDK**: What default polling interval and max retries should `get_document_results` use? Suggested: 1-second interval, 60 retries (1 minute timeout).
2. **ExtractedField table usage**: Deferred to future milestone. Single encrypted blob in `Document.encrypted_result` is sufficient for M2.
3. **Enclave key pair persistence across restarts**: In-flight documents will fail if enclave restarts mid-pipeline. Acceptable for MVP; noted as known compromise.
