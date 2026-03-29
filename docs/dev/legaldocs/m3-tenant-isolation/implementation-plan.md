---
date: 2026-03-29
author: claude-opus-4-6
status: draft
branch: main
related_research: docs/dev/legaldocs/m3-tenant-isolation/research.md
---

# Plan: Tenant Isolation (M3)

## Overview

Add multi-tenant access control to the LegalDocs API so that every document submission and retrieval is scoped to the requesting tenant. Tenants are identified by an `X-Tenant-ID` request header, enforced via a FastAPI dependency injected into every tenant-relevant endpoint. The database schema already has `tenant_id` columns and indexes in place -- this milestone wires up the enforcement layer at the API and client SDK levels.

The result: tenant A cannot see, list, or retrieve tenant B's documents. Requests without a valid `X-Tenant-ID` header are rejected with 400.

## Architecture Decisions

- **Header-based tenant identity**: Use `X-Tenant-ID` header (not request body) as the single source of tenant identity across all endpoints. Remove `tenant_id` from `DocumentSubmitRequest` body schema.
- **FastAPI dependency (not middleware)**: Implement tenant extraction as an injected dependency rather than middleware. This keeps `/health`, `/attestation`, and `/decrypt` unaffected and makes tenant_id explicitly available in endpoint signatures.
- **400 for missing header (not 422)**: Use `Header(None)` with manual validation to return 400, matching the press release spec. FastAPI's default `Header(...)` returns 422.
- **404 for cross-tenant access (not 403)**: Return 404 when a tenant requests another tenant's document to avoid leaking document existence.
- **No Celery task changes**: Tasks are internal, operate on validated document IDs, and are not externally callable. Tenant scoping at the API layer is sufficient for the MVP.
- **No `/decrypt` tenant scoping**: The `/decrypt` endpoint proxies to the enclave and has no DB interaction. Adding tenant enforcement would add complexity with no isolation benefit.
- **Simple list endpoint**: `GET /documents` returns all documents for the tenant with optional `status` query filter. No pagination for MVP.

## Prerequisites

- [ ] M0, M1, M2 complete (database schema, encryption, processing pipeline)
- [ ] `tenant_id` column and indexes exist on `documents` and `extracted_fields` tables (already done in M0)

---

## Phase 1: Tenant Dependency + API Endpoint Updates

### Goal

Add a `get_tenant_id` FastAPI dependency that extracts and validates `X-Tenant-ID` from request headers. Update all tenant-relevant endpoints to use it. Add the `GET /documents` list endpoint.

### Files to Modify

- `parent/api/dependencies.py` - Add `get_tenant_id` dependency
- `parent/api/schemas.py` - Remove `tenant_id` from `DocumentSubmitRequest`; add `DocumentListResponse`
- `parent/api/main.py` - Update `POST /documents` and `GET /documents/{id}`; add `GET /documents`

### Module Design

**`get_tenant_id` dependency** - Extracts and validates tenant identity from request header.

Key function:

- `get_tenant_id(x_tenant_id: str | None = Header(None)) -> str` - Returns validated tenant_id string or raises HTTPException(400) if missing/empty.

**Updated endpoints:**

- `POST /documents` - Inject `tenant_id` from header dependency (replace body field). Create `Document` with `tenant_id` from header.
- `GET /documents/{document_id}` - Inject `tenant_id` from header dependency. Add `.filter(Document.tenant_id == tenant_id)` to query. Return 404 if no match.
- `GET /documents` (new) - Inject `tenant_id` from header dependency. Query `Document` filtered by `tenant_id` with optional `status` query param. Return list of `DocumentStatusResponse`.

### Data Structures

```
DocumentSubmitRequest {
  encrypted_content: str  # base64-encoded (tenant_id removed)
  filename: Optional[str]
}

DocumentListResponse {
  documents: list[DocumentStatusResponse]
}
```

### Integration

- `get_tenant_id` is injected via `Depends(get_tenant_id)` alongside existing `Depends(get_db)` in endpoint signatures.
- `POST /documents` changes from `doc = Document(tenant_id=request.tenant_id, ...)` to `doc = Document(tenant_id=tenant_id, ...)` where `tenant_id` comes from the dependency.
- `GET /documents/{id}` adds `Document.tenant_id == tenant_id` to the existing query filter.
- `GET /documents` uses the composite index `ix_documents_tenant_id_status` when filtering by both tenant and status.

### Success Criteria

**Automated Verification:**

```bash
# Run linter/type checks per AGENTS.md
cd parent && python -m py_compile api/main.py api/dependencies.py api/schemas.py
```

**Manual Verification:**

- [ ] `POST /documents` without `X-Tenant-ID` header returns 400
- [ ] `POST /documents` with `X-Tenant-ID: acme` creates document with `tenant_id=acme`
- [ ] `GET /documents/{id}` with matching tenant returns document
- [ ] `GET /documents/{id}` with non-matching tenant returns 404
- [ ] `GET /documents` with `X-Tenant-ID: acme` returns only acme's documents
- [ ] `GET /documents?status=COMPLETED` filters within tenant
- [ ] Empty `X-Tenant-ID` header returns 400

---

## Phase 2: Client SDK Updates

### Goal

Update the client SDK to send `X-Tenant-ID` header on all tenant-scoped requests. Remove `tenant_id` from the JSON body. Add a `list_documents` method.

### Files to Modify

- `client/client.py` - Add `tenant_id` to constructor, send header, add `list_documents` method

### Module Design

**`LegalDocsClient` updates:**

- `__init__(self, api_url, tenant_id, ca_cert=None, client_cert=None, client_key=None)` - Add required `tenant_id` parameter, store as `self.tenant_id`.
- `_tenant_headers() -> dict` - Returns `{"X-Tenant-ID": self.tenant_id}` (internal helper).
- `submit_document(self, ciphertext, filename=None) -> str` - Remove `tenant_id` parameter. Send `X-Tenant-ID` header instead of body field. Remove `tenant_id` from JSON payload.
- `get_document_status(self, document_id) -> dict` - Add `X-Tenant-ID` header to GET request.
- `get_document_results(self, document_id, ...) -> dict` - Already calls `get_document_status`, which will now send the header.
- `list_documents(self, status=None) -> list[dict]` - New method. Calls `GET /documents` with `X-Tenant-ID` header and optional `status` query param.

### Integration

- The `tenant_id` is set once at client construction, included in all requests via the header helper.
- `submit_document` signature changes: `tenant_id` parameter is removed (breaking change -- callers must pass `tenant_id` to constructor instead).
- `get_attestation` and `decrypt_results` do not send the tenant header (no tenant scoping on those endpoints).

### Success Criteria

**Automated Verification:**

```bash
cd client && python -m py_compile client.py
```

**Manual Verification:**

- [ ] `LegalDocsClient(api_url, tenant_id="acme")` sets tenant_id
- [ ] `submit_document(ciphertext)` sends `X-Tenant-ID: acme` header and no `tenant_id` in body
- [ ] `get_document_status(doc_id)` sends `X-Tenant-ID: acme` header
- [ ] `list_documents()` calls `GET /documents` with header
- [ ] `list_documents(status="COMPLETED")` passes query param

---

## Phase 3: Integration Verification

### Goal

Verify end-to-end tenant isolation: two tenants submit documents, each sees only their own, and cross-tenant access is rejected.

### Files to Modify

- No new production code. Verification via manual test or script.

### Success Criteria

**Automated Verification:**

Run full project verification (compile checks on all modified files).

**Comprehensive E2E Tests (derived from Press Release user stories):**

1. **Tenant A submits and retrieves own document**: Client A (`X-Tenant-ID: acme`) submits a document, retrieves it by ID, sees it in list -- all succeed.
2. **Tenant B cannot access Tenant A's document**: Client B (`X-Tenant-ID: globex`) tries `GET /documents/{acme_doc_id}` -- receives 404.
3. **Tenant B's list excludes Tenant A's documents**: Client B calls `GET /documents` -- sees zero documents (or only their own).
4. **Missing header rejected**: Request without `X-Tenant-ID` to any scoped endpoint returns 400.
5. **Empty header rejected**: Request with `X-Tenant-ID: ""` returns 400.
6. **Two tenants coexist**: Both tenants submit documents, both can list only their own, counts match.

**Manual Verification:**

- [ ] Run full E2E with docker compose: two tenants, submit docs, verify isolation
- [ ] Confirm Celery pipeline still completes (status goes to COMPLETED)
- [ ] Verify `/health` and `/attestation` work without `X-Tenant-ID` header

---

## Rollback Plan

All changes are additive to existing endpoint signatures. To roll back:
1. Revert the three modified files (`main.py`, `schemas.py`, `dependencies.py`) and `client.py`.
2. No database migration is involved -- schema is unchanged.

## Open Questions

- Whether to add tenant header requirement to `/decrypt` for API consistency (deferred -- no isolation benefit for MVP).
- Default limit on `GET /documents` list endpoint (deferred -- return all for MVP).
- Whether to update demo scripts in `scripts/` to use the new client API (deferred to M4).
