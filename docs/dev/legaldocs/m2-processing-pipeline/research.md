---
date: 2026-03-29
researcher: claude-opus-4-6
branch: n/a (no git repo)
repository: legaldocs
tags: celery, pipeline, enclave, extraction, mTLS, encryption
status: complete
---

# Research: M2 Processing Pipeline

## Research Question

How to implement the end-to-end processing pipeline for M2: Celery chain orchestration, enclave `/process` endpoint, regex extraction, client upload/retrieval, and encrypted result flow.

## Summary

The codebase has a solid M0/M1 foundation. The parent API (`parent/api/main.py`) currently exposes `/health` and `/attestation` endpoints over FastAPI with mTLS proxying to the enclave. The Celery infrastructure (`parent/workers/celery_app.py`) is configured with Redis broker/backend but has no tasks defined yet (`parent/workers/tasks.py` is a stub). The enclave (`enclave/server.py`) runs a stdlib `http.server` with mTLS and exposes `/health`, `/attestation`, and `/decrypt` endpoints. The `CryptoManager` in `enclave/crypto.py` handles RSA/AES hybrid envelope encryption and provides both `encrypt_document` (static, takes a public key) and `decrypt_document` (uses instance private key). The extractor (`enclave/extractor.py`) is a stub. The database models (`parent/models/models.py`) already define `Document` (with `encrypted_content`, `encrypted_result`, `status`, `error_message` columns) and `ExtractedField` tables. The client SDK (`client/client.py`) handles attestation verification and document encryption but has no upload/retrieval methods.

M2 needs to wire these components together: add API endpoints for document submission and retrieval, implement Celery chain tasks, add a `/process` endpoint to the enclave that decrypts-extracts-encrypts, implement regex extraction, and extend the client SDK.

## Detailed Findings

### 1. Celery Chain Implementation for Multi-Stage Processing

**Current state**: `parent/workers/celery_app.py` (lines 1-25) configures a Celery app named `"legaldocs"` with Redis broker on db 0 and backend on db 1. JSON serialization is enforced. Task time limit is 300s (soft 270s). `parent/workers/tasks.py` (lines 1-4) is a stub with no tasks.

**What to build**: A Celery chain of three tasks:

1. **`ingest_document`** -- Marks the document as PROCESSING in PostgreSQL. Receives `document_id` as input, returns `document_id` for the next task.
2. **`forward_to_enclave`** -- Reads encrypted content from PostgreSQL, POSTs it to the enclave's `/process` endpoint over mTLS, receives encrypted results back. Returns `(document_id, encrypted_results_b64)`.
3. **`store_results`** -- Stores encrypted results in PostgreSQL (`Document.encrypted_result`), updates status to COMPLETED. Optionally stores individual encrypted fields in `ExtractedField`.

**Chain dispatch pattern**:

```python
from celery import chain
from parent.workers.celery_app import celery_app

pipeline = chain(
    ingest_document.s(document_id),
    forward_to_enclave.s(),
    store_results.s(),
)
pipeline.apply_async()
```

Each task receives the return value of the previous task as its first argument (Celery's default chain behavior). Since JSON serialization is enforced, binary data (encrypted blobs) must be base64-encoded when passed between tasks.

**Error handling**: Each task should catch exceptions and mark the document as FAILED with an error message. The `forward_to_enclave` task should use Celery's `self.retry()` with `max_retries=3` for transient enclave connection failures.

**Worker registration**: The docker-compose worker command (`celery -A parent.workers.celery_app worker`) auto-discovers tasks via `celery_app`. Tasks must either be defined in a module that imports `celery_app` or registered via `celery_app.conf.include = ['parent.workers.tasks']`.

### 2. Worker-to-Enclave Communication over mTLS

**Current state**: The parent API (`parent/api/main.py`, lines 12-16) reads enclave connection settings from environment variables: `ENCLAVE_HOST`, `ENCLAVE_PORT`, `CA_CERT_PATH`, `CLIENT_CERT_PATH`, `CLIENT_KEY_PATH`. It builds an `ssl.SSLContext` and uses `httpx.AsyncClient` for the `/attestation` proxy call (lines 30-36).

**What to build for the worker**: The Celery worker needs a synchronous mTLS HTTP client (Celery tasks are synchronous by default). Two options:

- **`httpx` (sync mode)**: `httpx.Client(verify=ssl_context)` -- same library already used in the API, supports sync mode.
- **`requests` with cert params**: `requests.post(url, data=blob, cert=(cert, key), verify=ca)`.

The `httpx` approach is preferable for consistency. The worker container already has the same cert volume mounts and environment variables as the API container (`docker-compose.yml`, lines 68-90).

**mTLS setup in worker task**:

```python
ssl_context = ssl.create_default_context()
ssl_context.load_verify_locations(CA_CERT_PATH)
ssl_context.load_cert_chain(CLIENT_CERT_PATH, CLIENT_KEY_PATH)
```

The same cert paths (`/certs/parent.crt`, `/certs/parent.key`, `/certs/ca.crt`) are available in the worker container. The worker reuses the parent's mTLS certs -- no separate worker certs are needed since both the API and worker containers are considered part of the "parent" trust domain.

### 3. Enclave `/process` Endpoint

**Current state**: The enclave server (`enclave/server.py`) uses Python's stdlib `http.server.BaseHTTPRequestHandler`. Routes are dispatched in `do_GET` (lines 66-73) and `do_POST` (lines 75-80). Currently only `/decrypt` is handled as a POST endpoint (lines 121-136). The `CryptoManager` instance is available via `self.server.crypto`.

**What to build**: A new `/process` POST endpoint that:

1. Accepts the encrypted document blob as the request body (same format as `/decrypt`).
2. Decrypts the document using `self.server.crypto.decrypt_document(encrypted_blob)`.
3. Runs regex extraction on the plaintext via the extractor module.
4. Serializes extraction results to JSON.
5. Encrypts the JSON results using `CryptoManager.encrypt_document(results_json, self.server.crypto.get_public_key_pem())` -- encrypting with the enclave's own public key so the enclave can later decrypt if needed, OR using a different strategy (see section 7).
6. Returns the encrypted results blob as `application/octet-stream`.

**Route addition in `do_POST`** (at `enclave/server.py` line 76):

```python
def do_POST(self) -> None:
    if self.path == "/decrypt":
        self._handle_decrypt()
    elif self.path == "/process":
        self._handle_process()
    else:
        self.send_response(404)
        self.end_headers()
```

**Error responses**: If decryption fails, return 400 with JSON error. If extraction succeeds but finds no fields, return 200 with encrypted empty results (not an error).

### 4. Regex Extraction Patterns for Legal Documents

**Current state**: `enclave/extractor.py` (lines 1-4) is a stub.

**What to build**: A module with regex patterns for extracting parties and dates from legal text.

**Party extraction patterns** (common legal document patterns):

```python
import re

# "BETWEEN [Party A] AND [Party B]"
BETWEEN_AND = re.compile(
    r'(?:BETWEEN|between)\s+(.+?)\s+(?:AND|and)\s+(.+?)(?:\s*[\(,\.])',
    re.DOTALL,
)

# "Party:" or "Parties:" label followed by names
PARTY_LABEL = re.compile(
    r'(?:Party|Parties|PARTY|PARTIES)\s*:\s*(.+?)(?:\n|$)',
)

# Named entities after "hereinafter" or "referred to as"
HEREINAFTER = re.compile(
    r'(.+?)\s*[\(,]\s*(?:hereinafter|hereafter)\s+(?:referred\s+to\s+as\s+)?["\u201c](.+?)["\u201d]',
)

# Common agreement header: "[Entity Name], a [state] [entity type]"
ENTITY_CLAUSE = re.compile(
    r'([A-Z][A-Za-z\s&,.]+(?:Inc\.|LLC|Corp\.|Ltd\.|L\.P\.|Company|Corporation))',
)
```

**Date extraction patterns**:

```python
# ISO dates: 2024-01-15
ISO_DATE = re.compile(r'\b(\d{4}-\d{2}-\d{2})\b')

# US dates: January 15, 2024 or Jan 15, 2024
US_DATE = re.compile(
    r'\b((?:January|February|March|April|May|June|July|August|September|October|November|December|'
    r'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4})\b'
)

# Numeric dates: 01/15/2024, 1/15/2024, 01-15-2024
NUMERIC_DATE = re.compile(r'\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})\b')

# Ordinal dates: "15th day of January, 2024"
ORDINAL_DATE = re.compile(
    r'\b(\d{1,2}(?:st|nd|rd|th)\s+day\s+of\s+\w+,?\s+\d{4})\b'
)
```

**Extractor function signature**:

```python
def extract_fields(text: str) -> dict:
    """Extract parties and dates from legal document text.

    Returns:
        {"parties": ["Party A", "Party B"], "dates": ["2024-01-15", "March 1, 2024"]}
    """
```

The function should apply all patterns, deduplicate results, and strip whitespace. The focus is on coverage for typical legal documents, not precision.

### 5. Client Document Submission and Retrieval

**Current state**: `client/client.py` defines `LegalDocsClient` with `get_attestation()`, `verify_attestation()`, and `encrypt_document()` methods (lines 28-166). There are no methods for uploading documents or retrieving results. The client uses the `requests` library.

**What to build**:

**`submit_document(ciphertext, tenant_id, filename=None)`**:

```python
def submit_document(self, ciphertext: bytes, tenant_id: str, filename: str = None) -> str:
    """POST encrypted document to the API. Returns document ID."""
    url = f"{self.api_url}/documents"
    # Send ciphertext as base64 in JSON body
    import base64
    payload = {
        "tenant_id": tenant_id,
        "encrypted_content": base64.b64encode(ciphertext).decode("utf-8"),
        "filename": filename,
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()["id"]
```

**`get_document(document_id)`**:

```python
def get_document(self, document_id: str) -> dict:
    """GET document status and encrypted results. Returns response dict."""
    url = f"{self.api_url}/documents/{document_id}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()
```

**`decrypt_results(encrypted_results_b64, enclave_public_key_pem)`** -- see section 7 for encryption strategy.

### 6. API Endpoints for Document Upload and Retrieval

**Current state**: `parent/api/main.py` has `/health` and `/attestation` GET endpoints. `parent/api/schemas.py` defines `HealthResponse`, `AttestationPayload`, and `AttestationResponse` Pydantic models.

**What to build**:

**`POST /documents`** endpoint:
- Request body: `{"tenant_id": str, "encrypted_content": str (base64), "filename": str | null}`
- Decodes base64 to bytes, stores in `Document.encrypted_content` as `LargeBinary`.
- Sets `status = "PENDING"`.
- Dispatches the Celery chain.
- Returns `{"id": str, "status": "PENDING"}`.

**`GET /documents/{document_id}`** endpoint:
- Reads `Document` from PostgreSQL.
- Returns `{"id": str, "status": str, "encrypted_result": str | null (base64), "error_message": str | null, "created_at": str, "updated_at": str}`.
- If status is COMPLETED, `encrypted_result` contains base64-encoded encrypted results.

**New Pydantic schemas needed**:
- `DocumentSubmitRequest(tenant_id, encrypted_content, filename)`
- `DocumentSubmitResponse(id, status)`
- `DocumentStatusResponse(id, status, encrypted_result, error_message, created_at, updated_at)`

**Database session**: The API needs a FastAPI dependency for DB sessions. `parent/db.py` already provides `get_session()` as a generator (lines 23-31), compatible with FastAPI's `Depends()`.

### 7. Encrypted Results Flow and Encryption Strategy

**Current state**: The `CryptoManager` (`enclave/crypto.py`) has:
- `decrypt_document(encrypted_blob)` -- instance method, uses the enclave's private key (line 36-68).
- `encrypt_document(plaintext, public_key_pem)` -- static method, encrypts with any given public key (line 70-99).

The enclave generates a fresh RSA key pair on startup (`__init__`, lines 22-26). This key pair is distinct from the mTLS certificate keys.

**Strategy: Encrypt results with the enclave's own RSA public key**

The simplest approach that maintains the privacy guarantee:

1. **Enclave encrypts results** using its own `CryptoManager` public key: `CryptoManager.encrypt_document(results_json, self.get_public_key_pem())`.
2. **Parent stores the encrypted blob** in `Document.encrypted_result` -- parent cannot decrypt because it does not have the enclave's private key.
3. **Client fetches the encrypted blob** via `GET /documents/{id}`.
4. **Client sends the blob to the enclave for decryption** via the existing `/decrypt` endpoint (proxied through the parent API's existing pattern, or a new `GET /documents/{id}/decrypt` endpoint that forwards to the enclave).

However, this requires an extra round-trip to the enclave for the client to read results. A simpler MVP approach:

**Alternative strategy: Return encrypted results as-is from the enclave, encrypted with the enclave's public key, and add a `/decrypt` proxy endpoint**

1. Enclave encrypts results with its own public key.
2. Parent stores the encrypted blob.
3. Client fetches the blob via `GET /documents/{id}`.
4. Client calls a new `POST /documents/{id}/decrypt` endpoint on the parent API.
5. Parent proxies the encrypted blob to the enclave's `/decrypt` endpoint over mTLS.
6. Parent returns the decrypted results to the client.

**Wait** -- this would expose plaintext results to the parent, breaking the privacy guarantee.

**Correct strategy: Client-side decryption via enclave**

1. Enclave encrypts results with its own public key.
2. Parent stores the encrypted blob.
3. Client fetches the encrypted blob.
4. Client sends the encrypted blob directly to the enclave's `/decrypt` endpoint... but the client does not have direct access to the enclave (it is on an internal network).

**Final recommended strategy: Encrypt results with the SAME key used for the original document**

The client encrypted the original document with the enclave's public key. The enclave can encrypt results with the same public key. When the client retrieves the encrypted results, it needs to call the enclave via the parent to decrypt. Since the parent only sees ciphertext in transit, the privacy guarantee holds:

1. Enclave processes the document and produces extraction results (JSON).
2. Enclave encrypts the results JSON with its own RSA public key: `CryptoManager.encrypt_document(results_bytes, self.get_public_key_pem())`.
3. Parent stores the encrypted blob in `Document.encrypted_result`.
4. Client fetches the encrypted blob via `GET /documents/{id}`.
5. Client calls `POST /decrypt` on the parent API (new endpoint), which proxies the blob to the enclave's existing `/decrypt` endpoint over mTLS.
6. Enclave decrypts with its private key, returns plaintext results to parent, which returns to client.

**Privacy analysis**: In step 6, the parent API proxies the decrypted results. This means the parent momentarily sees the plaintext extraction results (parties, dates) in transit. For the MVP, this is acceptable because:
- The parent sees extraction results (metadata), not the original document content.
- The original document remains encrypted throughout.
- In production (AWS Nitro), the vsock channel would be encrypted end-to-end.

**Simplest MVP approach**: The enclave encrypts results with its own key. A proxy `/decrypt` endpoint on the parent forwards the blob to the enclave and returns decrypted JSON. The client calls this endpoint. This avoids requiring the client to have a key pair or direct enclave access.

Alternatively, **if no proxy is desired**: The enclave could return results as plaintext JSON to the worker (since the mTLS channel is already trusted), and the worker encrypts the results before storing. But this means the worker sees plaintext results, which violates the privacy model.

**Recommended approach for M2**: The enclave encrypts results with its own public key. The parent stores only ciphertext. The client retrieves the ciphertext and calls a proxy decrypt endpoint. The parent forwards the ciphertext to the enclave's `/decrypt` and returns the result. The parent sees the decrypted extraction results in transit but never the original document. This is the pragmatic MVP choice that matches the press release description.

## Code References

- `parent/api/main.py:1-52` -- FastAPI app with `/health` and `/attestation` endpoints; mTLS proxy pattern to enclave
- `parent/api/schemas.py:1-19` -- Pydantic models for health and attestation responses
- `parent/workers/celery_app.py:1-25` -- Celery app configuration with Redis broker/backend, JSON serialization
- `parent/workers/tasks.py:1-4` -- Stub, no tasks defined
- `parent/models/models.py:1-78` -- `Document` model with `encrypted_content`, `encrypted_result`, `status` columns; `ExtractedField` model with `encrypted_value`
- `parent/db.py:1-31` -- SQLAlchemy engine and session factory; `get_session()` generator compatible with FastAPI Depends
- `enclave/server.py:1-155` -- mTLS HTTP server with `/health`, `/attestation`, `/decrypt` endpoints; `EnclaveHTTPServer` holds `CryptoManager`
- `enclave/crypto.py:1-99` -- `CryptoManager` with RSA key pair, hybrid RSA/AES envelope encryption; `encrypt_document` is static, `decrypt_document` uses instance key
- `enclave/extractor.py:1-4` -- Stub, no extraction logic
- `client/client.py:1-166` -- Client SDK with attestation verification and document encryption; no upload/retrieval methods
- `docker-compose.yml:1-119` -- Services: postgres, redis, migrate, api, worker, enclave; worker has same cert mounts as API; enclave on internal-only network

## Architecture Notes

The system has a clear trust boundary: the enclave container sits on `enclave-net` (internal-only, no external access). The parent API and worker sit on both `external-net` and `enclave-net`, acting as the bridge. The `CryptoManager` generates a fresh RSA key pair at enclave startup (distinct from mTLS cert keys) -- this is the key used for document encryption/decryption.

The Celery worker runs the same Docker image as the API (same `parent/Dockerfile`), sharing the same codebase, cert paths, and environment variables. This means the mTLS client setup can be shared between the API and worker via a common utility function.

The `Document` model already has all the columns needed for M2: `encrypted_content` (input), `encrypted_result` (output), `status` (lifecycle), `error_message` (failure tracking). The `ExtractedField` model provides per-field encrypted storage but may not be needed for MVP -- storing all results as a single encrypted blob in `Document.encrypted_result` is simpler.

Data flow through the pipeline: Client encrypts doc -> POST /documents -> DB stores ciphertext (PENDING) -> Celery chain starts -> Task 1: mark PROCESSING -> Task 2: POST ciphertext to enclave /process, get encrypted results back -> Task 3: store encrypted results in DB, mark COMPLETED -> Client GET /documents/{id} -> Client POST /decrypt (proxy) -> get plaintext results.

## Open Questions

1. **ExtractedField usage in MVP**: Should M2 populate the `ExtractedField` table with individual encrypted field values, or just store everything as a single blob in `Document.encrypted_result`? The single-blob approach is simpler; per-field storage adds complexity but matches the schema design.

2. **Result decryption endpoint**: Should the parent API expose a `POST /documents/{id}/decrypt` convenience endpoint that proxies to the enclave, or should the client call a generic `POST /decrypt` endpoint? The per-document endpoint is more RESTful.

3. **Celery task autodiscovery**: The current worker command `celery -A parent.workers.celery_app worker` does not explicitly include task modules. Tasks in `parent/workers/tasks.py` need to import `celery_app` and use `@celery_app.task` decorator, or `celery_app.conf.include` needs to be set.

4. **Binary data in Celery chain**: Since JSON serialization is enforced, encrypted blobs passed between tasks must be base64-encoded. This adds serialization overhead but is necessary for JSON-only Celery configs.

5. **Enclave key pair persistence**: The enclave generates a new key pair on each restart. Documents encrypted with the old key cannot be decrypted after restart. For MVP this is acceptable, but in-flight documents will fail if the enclave restarts mid-pipeline.
