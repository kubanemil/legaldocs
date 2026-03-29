# Tenant Isolation - PR FAQ

> **Milestone**: M3
> **Created**: 2026-03-29
> **Status**: Draft

---

## Press Release

### Tenant Isolation

**For** organizations and teams sharing a single deployment of the legal document processing pipeline
**Who** need assurance that their encrypted documents and extraction results are completely invisible to other tenants on the same system
**The** Tenant Isolation feature
**Is a** new access control layer across all API endpoints
**That** guarantees every document submission, status check, and result retrieval is scoped to the requesting tenant -- so tenant A can never see, list, or retrieve tenant B's documents or results
**Unlike** the current state where any client with a valid document ID can retrieve any document regardless of ownership
**Our solution** enforces tenant scoping at every API boundary using a simulated `X-Tenant-ID` header, with all database queries filtered by tenant, ensuring data isolation without requiring a separate deployment per customer.

---

Today we are announcing Tenant Isolation for the legal document processing pipeline. Every API endpoint now enforces multi-tenant access control, ensuring that documents and extraction results are only accessible to the tenant that submitted them.

Clients include an `X-Tenant-ID` header with every request. When a client submits a document via `POST /documents`, the system records the tenant association. When retrieving a document via `GET /documents/{id}`, the system verifies that the requesting tenant owns that document -- if not, the request is rejected as if the document does not exist. Listing endpoints such as `GET /documents` return only documents belonging to the requesting tenant. All database queries are scoped by `tenant_id`, so there is no code path that can leak data across tenant boundaries. This is a simulated tenant model (no real authentication or token validation) designed to prove the isolation pattern that would be backed by a real identity provider in production. The `tenant_id` column already exists in the database schema from M0, so this milestone wires up the enforcement layer that was deferred until now.

---

## FAQ

### Customer Experience

Q: How does a client specify their tenant identity?
A: The client includes an `X-Tenant-ID` header on every API request. This is a simulated model -- in production, the tenant identity would be derived from an authenticated token (e.g., JWT claims). For this MVP, the header is trusted as-is.

Q: What does the happy path look like step-by-step?
A: (1) Client sets the `X-Tenant-ID: acme-corp` header. (2) Client calls `POST /documents` with encrypted ciphertext. The document is stored with `tenant_id=acme-corp`. (3) Client calls `GET /documents/{id}` with `X-Tenant-ID: acme-corp` and receives the document. (4) A different client with `X-Tenant-ID: globex` calls `GET /documents/{id}` for the same document and receives a 404 Not Found. (5) Client calls `GET /documents` with `X-Tenant-ID: acme-corp` and sees only their own documents.

Q: What happens if a client omits the `X-Tenant-ID` header?
A: The API rejects the request with a 400 Bad Request error. No endpoint is accessible without a tenant identifier.

Q: What happens if a client tries to access a document belonging to another tenant?
A: The API returns 404 Not Found. The system does not distinguish between "document does not exist" and "document belongs to another tenant" -- this prevents information leakage about other tenants' document IDs.

Q: How does this interact with the existing processing pipeline (M2)?
A: The pipeline continues to work exactly as before. The only change is that the `tenant_id` is now enforced at the API layer. Celery tasks already carry the `tenant_id` from document creation. The enclave is unaffected -- it processes ciphertext without any tenant awareness.

### Scope & Non-Goals

Q: What is explicitly NOT included in V1?
A: This milestone does NOT include: real authentication or authorization (no JWT, no OAuth, no API keys), tenant provisioning or management endpoints, per-tenant rate limiting or quotas, tenant-level encryption key separation (all tenants share the enclave's key pair), row-level security (RLS) in PostgreSQL, or audit logging of cross-tenant access attempts.

Q: What is the minimal version that still delivers the core benefit?
A: All existing API endpoints (`POST /documents`, `GET /documents/{id}`, `GET /documents`) require an `X-Tenant-ID` header, all database queries include a `WHERE tenant_id = :tenant_id` clause, and accessing another tenant's document returns 404. This is sufficient to prove the isolation pattern.

Q: What are we deferring to future iterations?
A: Real identity provider integration, per-tenant encryption keys, PostgreSQL row-level security policies, tenant administration (create/disable tenants), and cross-tenant admin views for operators.

### Technical Constraint FAQs

Q: What existing system capabilities does this depend on?
A: This depends on M0 (database schema with `tenant_id` column and indexes already in place), M1 (encryption infrastructure), and M2 (processing pipeline with `POST /documents` and `GET /documents` endpoints). The `tenant_id` column is already defined in the documents and extracted_fields tables with indexes.

Q: What new capabilities need to be built?
A: (1) Middleware or dependency that extracts and validates `X-Tenant-ID` from request headers. (2) Tenant scoping on all existing API endpoints -- `POST /documents`, `GET /documents/{id}`, and `GET /documents` (list). (3) All database queries updated to filter by `tenant_id`. (4) Client SDK updated to include `X-Tenant-ID` header in all requests.

Q: What is the simplest implementation that delivers the PR outcome?
A: A FastAPI dependency that extracts `X-Tenant-ID` from the request header (returning 400 if missing), injected into every endpoint. Each endpoint passes the `tenant_id` to its database query. The existing `tenant_id` column and indexes in the schema are already sufficient -- no migration needed.

Q: Are there performance considerations?
A: No. The `tenant_id` column is already indexed (including a composite index on `tenant_id` + `status`). Adding a WHERE clause on an indexed column has negligible performance impact for MVP-scale data.

### Edge Cases & Error States FAQs

Q: What happens if the `X-Tenant-ID` header contains an empty string?
A: The API rejects the request with a 400 Bad Request error. The tenant ID must be a non-empty string.

Q: What happens if a tenant has no documents?
A: The `GET /documents` endpoint returns an empty list. This is a normal and valid state.

Q: What happens if a document was submitted before tenant isolation was enforced?
A: Documents already have a `tenant_id` set at creation time (from the `POST /documents` request body). The enforcement layer simply adds filtering on retrieval. Existing documents are already associated with a tenant.

Q: What happens if two tenants submit identical documents?
A: Each submission creates a separate document record with its own `tenant_id`. The documents are completely independent. Each tenant sees only their own copy.

### Business & Operational FAQs

Q: How will we know if this is successful?
A: Success means: (1) Tenant A submits a document and can retrieve it. (2) Tenant B cannot retrieve tenant A's document (receives 404). (3) Tenant B's document listing does not include tenant A's documents. (4) All API endpoints reject requests without an `X-Tenant-ID` header. (5) No database query in the application can return documents across tenant boundaries.

Q: What could go wrong post-launch?
A: A new endpoint added in the future might forget to include tenant scoping. A database query might be added without the `tenant_id` filter. The simulated tenant model could give a false sense of security if someone treats it as real authentication.

Q: Are there compliance or security considerations?
A: The simulated tenant model (trusting the `X-Tenant-ID` header) is explicitly not secure -- any client can impersonate any tenant by setting the header. This is acceptable for the MVP because the goal is to prove the isolation pattern at the data layer, not to implement real authentication. Production would replace the header with JWT-derived tenant identity.

---

## Implementation Scope

### Current State -> End State

| Current State | End State |
| --- | --- |
| API endpoints accept `tenant_id` in the request body for document creation but do not enforce it on retrieval | All API endpoints require `X-Tenant-ID` header and scope every operation to that tenant |
| `GET /documents/{id}` returns any document regardless of who is requesting | `GET /documents/{id}` returns 404 if the document does not belong to the requesting tenant |
| No document listing endpoint exists, or if it does, it returns all documents | `GET /documents` returns only documents belonging to the requesting tenant |
| Database queries do not consistently filter by `tenant_id` on read paths | Every database query that returns documents or results includes a `WHERE tenant_id` filter |
| Client SDK does not send a tenant header | Client SDK includes `X-Tenant-ID` header in all API calls |

### User Stories

- [ ] As a client, I submit documents with a tenant identifier via the `X-Tenant-ID` header so that the system knows which tenant owns the document (M3.1)
- [ ] As a client, I can only retrieve documents belonging to my tenant -- attempts to access another tenant's document return 404 (M3.2)
- [ ] As the system, all database queries are scoped by `tenant_id` so that no code path can leak data across tenant boundaries (M3.3)
- [ ] As a client, I can list my documents and see only the ones belonging to my tenant
- [ ] As the system, I reject any API request that does not include a valid `X-Tenant-ID` header with a 400 error

### Success Criteria

- [ ] All API endpoints require the `X-Tenant-ID` header; requests without it receive 400
- [ ] `POST /documents` associates the document with the tenant from the header
- [ ] `GET /documents/{id}` returns 404 when a different tenant requests the document
- [ ] `GET /documents` returns only documents belonging to the requesting tenant
- [ ] All database queries that read documents or extracted fields include `tenant_id` in the WHERE clause
- [ ] Client SDK sends `X-Tenant-ID` header with every request
- [ ] A demo can show two tenants submitting documents and each seeing only their own

### Known Compromises

- [ ] Simulated tenant model (header-based, no auth) -- acceptable because the goal is proving the isolation pattern, not real security; production would use JWT-derived tenant identity
- [ ] No row-level security in PostgreSQL -- application-level filtering only; acceptable for MVP since all access goes through the API layer
- [ ] No audit trail for cross-tenant access attempts -- acceptable for MVP; the 404 response prevents leakage but does not log the attempt
- [ ] Tenant ID is a free-form string with no provisioning -- acceptable for MVP simplicity

### Open Questions / Needs Research

- [ ] Should tenant scoping be implemented as a FastAPI middleware or as an injected dependency on each endpoint?
- [ ] Does the existing `POST /documents` endpoint already read `tenant_id` from the request body, and should it switch to reading from the header instead (or accept both)?
- [ ] Are there any existing database queries (e.g., in Celery tasks) that read documents without tenant scoping that need to be updated?
- [ ] Should the `GET /documents` list endpoint support pagination or filtering by status within a tenant?

---

## Next Steps

When ready to implement:

1. Run `/pipe-research --project legaldocs --ref m3 tenant isolation` to analyze codebase
2. Run `/pipe-plan-implementation --project legaldocs --ref m3 tenant isolation` to create implementation plan
3. Run `/pipe-implement docs/dev/legaldocs/m3-tenant-isolation/implementation-plan.md` to execute the plan
