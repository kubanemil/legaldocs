---
date: 2026-03-29
researcher: claude-opus-4-6
branch: main
repository: legaldocs
tags: tenant-isolation, multi-tenant, fastapi, x-tenant-id, m3
status: complete
---

# Research: Tenant Isolation (M3)

## Research Question

How to implement tenant isolation via `X-Tenant-ID` header across all API endpoints, including FastAPI dependency extraction, database query scoping, tenant validation in Celery tasks, and client SDK updates.

## Summary

The codebase already has full tenant infrastructure at the data layer: both `documents` and `extracted_fields` tables include a `tenant_id` column with single-column and composite indexes already in place (migration `001_initial_schema.py`). The `Document` model defines `tenant_id` as a non-nullable `String(64)` column. The `POST /documents` endpoint currently reads `tenant_id` from the JSON request body (`DocumentSubmitRequest.tenant_id`) and writes it to the database. However, no retrieval endpoint filters by tenant -- `GET /documents/{id}` queries by document ID alone, and there is no list endpoint (`GET /documents`).

The Celery task chain (`ingest_document`, `forward_to_enclave`, `store_results`) queries documents by ID only, with no tenant scoping. Since these tasks operate on documents that were already validated at the API layer and use internal document IDs, tenant scoping in tasks is optional but could serve as defense-in-depth. The client SDK (`LegalDocsClient`) sends `tenant_id` in the JSON body of `submit_document` but does not send any tenant header on `get_document_status` or `get_document_results` calls.

The implementation path is straightforward: add a FastAPI dependency that extracts and validates `X-Tenant-ID` from request headers, inject it into every endpoint, scope all DB queries by `tenant_id`, add a `GET /documents` list endpoint, and update the client SDK to send the header.

## Detailed Findings

### 1. Current Tenant Handling in API Endpoints

**File: `parent/api/main.py`**

- **`POST /documents` (line 68-106)**: Accepts `tenant_id` from the request body via `DocumentSubmitRequest.tenant_id` (line 87). Writes it to the `Document` row. Does not read from any header.
- **`GET /documents/{document_id}` (line 109-130)**: Queries `Document` by `id` only (line 115: `db.query(Document).filter(Document.id == document_id).first()`). No tenant filtering whatsoever. Any client with a valid document ID can retrieve any document.
- **`GET /attestation` (line 39-65)**: No tenant relevance -- proxies to enclave.
- **`POST /decrypt` (line 133-177)**: No tenant relevance -- proxies encrypted data to enclave.
- **`GET /health` (line 33-35)**: No tenant relevance.
- **No `GET /documents` list endpoint exists.** The press release specifies one is needed.

### 2. Request Schema: `DocumentSubmitRequest`

**File: `parent/api/schemas.py` (line 27-30)**

```python
class DocumentSubmitRequest(BaseModel):
    tenant_id: str
    encrypted_content: str  # base64-encoded
    filename: Optional[str] = None
```

The `tenant_id` field is currently a required body parameter. The press release specifies switching to `X-Tenant-ID` header for all endpoints. Decision needed: remove `tenant_id` from the body schema and source it exclusively from the header, or keep both and validate they match. The simpler approach (per press release) is to source exclusively from the header and remove `tenant_id` from `DocumentSubmitRequest`.

### 3. Database Schema and Indexes

**File: `parent/models/models.py`**

- `Document.tenant_id`: `Column(String(64), nullable=False, index=True)` (line 26)
- `ExtractedField.tenant_id`: `Column(String(64), nullable=False, index=True)` (line 69)
- Composite index: `Index("ix_documents_tenant_id_status", "tenant_id", "status")` (line 55)

**File: `alembic/versions/001_initial_schema.py`**

- `ix_documents_tenant_id` index on `documents.tenant_id` (line 38)
- `ix_documents_tenant_id_status` composite index on `(tenant_id, status)` (line 40)
- `ix_extracted_fields_tenant_id` index on `extracted_fields.tenant_id` (line 52)

No migration is needed. The schema is already prepared for tenant-scoped queries.

### 4. FastAPI Dependencies

**File: `parent/api/dependencies.py`**

Two dependencies exist:
- `get_db()` (line 9-19): Yields a SQLAlchemy session. Standard pattern.
- `get_config()` (line 22-34): Returns config dict from env vars.

No tenant extraction dependency exists. A new dependency is needed:

```python
from fastapi import Header, HTTPException

def get_tenant_id(x_tenant_id: str = Header(...)) -> str:
    """Extract and validate X-Tenant-ID from request header."""
    if not x_tenant_id or not x_tenant_id.strip():
        raise HTTPException(status_code=400, detail="X-Tenant-ID header is required")
    return x_tenant_id.strip()
```

FastAPI's `Header(...)` automatically maps the `x_tenant_id` parameter to the `X-Tenant-ID` HTTP header (converting underscores to hyphens). Using `...` (Ellipsis) makes it required -- FastAPI returns 422 if missing. To return 400 instead (per press release), explicit validation with a default of `Header(None)` and manual check is needed, or a custom exception handler for `RequestValidationError` could remap 422 to 400.

### 5. Celery Tasks and Tenant Scoping

**File: `parent/workers/tasks.py`**

All three tasks query by document ID only:

- `ingest_document` (line 63): `session.query(Document).filter(Document.id == document_id).first()`
- `forward_to_enclave` (line 108): `session.query(Document).filter(Document.id == document_id).first()`
- `store_results` (line 177): `session.query(Document).filter(Document.id == document_id).first()`

These tasks receive only `document_id` as input (a UUID string). They do not receive `tenant_id`. The task chain is dispatched from `submit_document` in `main.py` (line 99-104):

```python
pipeline = chain(
    ingest_document.s(document_id),
    forward_to_enclave.s(),
    store_results.s(),
)
```

**Options for tenant scoping in tasks:**

1. **No change (minimal approach)**: Tasks operate on documents that were already tenant-validated at the API layer. The document ID is a UUID that is not guessable. The tasks are internal and not directly callable by external clients.
2. **Pass tenant_id through the chain**: Modify `ingest_document.s(document_id, tenant_id)` and add `tenant_id` to the WHERE clause in each task. This is defense-in-depth.
3. **Read tenant_id from the document record**: The first task loads the document; subsequent tasks could verify the tenant matches. This adds no new data flow but requires knowing the expected tenant.

The press release states: "Celery tasks already carry the tenant_id from document creation. The enclave is unaffected." This suggests option 1 or 2. Since the tasks already load the document (which has `tenant_id` set), and the tasks are not externally accessible, option 1 (no change) is the simplest path that meets the press release outcome.

### 6. Client SDK

**File: `client/client.py`**

- `submit_document` (line 169-198): Sends `tenant_id` in the JSON body. Does not send any `X-Tenant-ID` header.
- `get_document_status` (line 200-213): No tenant header sent. Plain GET request.
- `get_document_results` (line 215-261): Calls `get_document_status` in a loop. No tenant header.
- `decrypt_results` (line 263-282): Calls `POST /decrypt`. No tenant relevance (enclave-proxied).
- `get_attestation` (line 49-55): No tenant relevance.

**Changes needed:**
- Add a `tenant_id` instance attribute (set in `__init__` or per-call).
- Include `X-Tenant-ID` header in all requests to tenant-scoped endpoints.
- Remove `tenant_id` from the JSON body in `submit_document` (if switching fully to header-based).
- Add a `list_documents` method that calls `GET /documents`.

The `__init__` method (line 29-46) currently accepts `api_url`, `ca_cert`, `client_cert`, `client_key`. A `tenant_id` parameter would be added.

### 7. Endpoints That Need Tenant Scoping

| Endpoint | Current State | Needed Change |
|---|---|---|
| `POST /documents` | Reads `tenant_id` from body | Read from `X-Tenant-ID` header; remove from body |
| `GET /documents/{id}` | No tenant filtering | Add `WHERE tenant_id = :tenant_id` |
| `GET /documents` | Does not exist | Create with `WHERE tenant_id = :tenant_id` |
| `GET /attestation` | No tenant relevance | No change needed |
| `POST /decrypt` | No tenant relevance | No change needed (or optionally require header for consistency) |
| `GET /health` | No tenant relevance | No change needed |

### 8. Error Response Behavior

The press release specifies:
- Missing `X-Tenant-ID` header: **400 Bad Request**
- Accessing another tenant's document: **404 Not Found** (not 403, to avoid leaking existence)
- Empty `X-Tenant-ID` header: **400 Bad Request**

FastAPI's built-in `Header(...)` returns 422 for missing required headers. To get 400, either:
1. Use `Header(None)` (optional) and manually validate, raising `HTTPException(400)`.
2. Add a global exception handler that converts `RequestValidationError` to 400 for header-related errors.

Option 1 is simpler and more explicit.

## Code References

- `parent/api/main.py:68-106` - `POST /documents` endpoint, reads `tenant_id` from request body
- `parent/api/main.py:109-130` - `GET /documents/{id}` endpoint, no tenant filtering
- `parent/api/schemas.py:27-30` - `DocumentSubmitRequest` with `tenant_id` body field
- `parent/api/dependencies.py:9-19` - `get_db()` dependency, where tenant dependency will be added
- `parent/models/models.py:26` - `Document.tenant_id` column definition
- `parent/models/models.py:55` - Composite index on `(tenant_id, status)`
- `parent/models/models.py:69` - `ExtractedField.tenant_id` column definition
- `parent/workers/tasks.py:63` - `ingest_document` queries by `Document.id` only
- `parent/workers/tasks.py:108` - `forward_to_enclave` queries by `Document.id` only
- `parent/workers/tasks.py:177` - `store_results` queries by `Document.id` only
- `client/client.py:169-198` - `submit_document` sends `tenant_id` in body, no header
- `client/client.py:200-213` - `get_document_status` sends no tenant identifier
- `alembic/versions/001_initial_schema.py:38-40` - Tenant indexes already in place

## Architecture Notes

The current architecture has a clean separation:
- **API layer** (`parent/api/`): FastAPI endpoints with dependency injection for DB sessions.
- **Worker layer** (`parent/workers/`): Celery tasks that create their own DB sessions via `_get_db_session()`.
- **Data layer** (`parent/models/`): SQLAlchemy models with tenant columns and indexes pre-built.
- **Client SDK** (`client/`): Standalone `requests`-based SDK.

Tenant isolation fits naturally into the API layer via FastAPI's dependency injection. The `get_tenant_id` dependency can be injected alongside `get_db` in each endpoint. DB queries gain a `.filter(Document.tenant_id == tenant_id)` clause. The worker layer does not need changes for the minimal implementation since tasks operate on validated document IDs.

The enclave is completely tenant-unaware -- it processes encrypted bytes without any notion of tenants. This is correct and should remain unchanged.

## Answers to Open Questions from Press Release

**Q: Should tenant scoping be implemented as a FastAPI middleware or as an injected dependency on each endpoint?**
A: Dependency injection is the better fit. Middleware runs on every request including `/health` and `/attestation`, which do not need tenant scoping. A dependency is opt-in per endpoint, explicit, and testable. It also gives each endpoint direct access to the `tenant_id` value for use in DB queries.

**Q: Does the existing `POST /documents` endpoint already read `tenant_id` from the request body, and should it switch to reading from the header instead?**
A: Yes, `POST /documents` reads `tenant_id` from `DocumentSubmitRequest.tenant_id` (body field). It should switch to reading from the `X-Tenant-ID` header exclusively, and `tenant_id` should be removed from `DocumentSubmitRequest`. This ensures a single, consistent source of tenant identity across all endpoints.

**Q: Are there any existing database queries in Celery tasks that read documents without tenant scoping that need to be updated?**
A: Yes, all three Celery tasks (`ingest_document`, `forward_to_enclave`, `store_results`) query by `Document.id` without tenant scoping. However, since these tasks are internal (dispatched by the API after tenant validation), receive only `document_id` (not user input), and are not externally callable, updating them is optional. The minimal approach is to leave them as-is. Defense-in-depth would pass `tenant_id` through the chain.

**Q: Should the `GET /documents` list endpoint support pagination or filtering by status within a tenant?**
A: For the MVP, a simple list with optional `status` query parameter is sufficient. The composite index `ix_documents_tenant_id_status` on `(tenant_id, status)` already supports this query efficiently. Pagination can be deferred.

## Open Questions

- Whether to add `X-Tenant-ID` requirement to `/decrypt` endpoint for consistency (currently tenant-irrelevant but could enforce header for uniformity across all non-health endpoints).
- Whether the `GET /documents` list endpoint should have a default limit or return all documents for the tenant (MVP simplicity vs. production habit).
