# Processing Pipeline - PR FAQ

> **Milestone**: M2
> **Created**: 2026-03-29
> **Status**: Draft

---

## Press Release

### Processing Pipeline

**For** developers and organizations processing sensitive legal documents at scale
**Who** need an end-to-end pipeline that accepts encrypted documents, extracts structured data inside a secure enclave, and returns encrypted results -- all without the platform ever seeing plaintext
**The** Processing Pipeline
**Is a** new capability built on the Enclave + Encryption foundation (M1)
**That** lets clients submit encrypted legal documents via a simple API, have them automatically processed through a Celery-based pipeline that forwards ciphertext to the enclave for decryption, extraction, and re-encryption, and retrieve the encrypted results when ready
**Unlike** manually coordinating encryption, enclave communication, and result retrieval as separate steps
**Our solution** chains the entire flow into an automated pipeline where the client uploads once, polls for completion, and downloads encrypted results -- while the parent API, database, and task queue never handle plaintext at any stage.

---

Today we are announcing the Processing Pipeline for the legal document processing system: the first complete end-to-end document processing flow with privacy guarantees.

Clients can now submit an encrypted legal document to the `POST /documents` API endpoint and receive a document ID. Behind the scenes, the parent API stores the encrypted blob in PostgreSQL with a PENDING status and queues a Celery task. The Celery worker -- running on the parent side -- picks up the task and forwards the ciphertext to the enclave over the existing mTLS connection. The enclave decrypts the document, extracts parties and dates using regex, encrypts the extraction results with the client's context, and returns the encrypted results to the worker. The worker stores the encrypted results in PostgreSQL and updates the document status to COMPLETED. The client can then call `GET /documents/{id}` to retrieve the encrypted results and decrypt them locally. At no point does the parent API, the database, or the task queue see plaintext -- only ciphertext passes through the system outside the enclave boundary. This milestone transforms the encryption primitives from M1 into a working, automated processing pipeline using Celery chains for ordered stage processing.

---

## FAQ

### Customer Experience

Q: How does a client submit a document for processing?
A: The client encrypts the document locally using the enclave's public key (obtained via attestation in M1), then calls `POST /documents` with the ciphertext. The API returns a document ID that the client uses to check status and retrieve results.

Q: What does the happy path look like step-by-step?
A: (1) Client encrypts a legal document using the enclave's public key. (2) Client calls `POST /documents` with the ciphertext. (3) API stores the encrypted blob in PostgreSQL with status=PENDING and returns a document ID. (4) API queues a Celery task for processing. (5) Celery worker forwards the ciphertext to the enclave over mTLS. (6) Enclave decrypts the document, extracts parties and dates via regex, encrypts the results. (7) Enclave returns encrypted results to the worker. (8) Worker stores encrypted results in PostgreSQL, updates status to COMPLETED. (9) Client calls `GET /documents/{id}` and receives the encrypted results. (10) Client decrypts the results locally.

Q: How does the client know when processing is complete?
A: The client polls `GET /documents/{id}`. The response includes the document's status (PENDING, PROCESSING, COMPLETED, or FAILED). When status is COMPLETED, the encrypted results are included in the response.

Q: What fields are extracted from legal documents?
A: The enclave extracts parties (names of individuals or organizations) and dates using regex patterns. The focus of this milestone is the pipeline architecture, not extraction quality -- regex is sufficient to prove the end-to-end flow works.

### Scope & Non-Goals

Q: What is explicitly NOT included in V1?
A: This milestone does NOT include: tenant isolation or multi-tenant scoping (M3), NLP or ML-based extraction (regex only), webhook/callback notifications for completion (polling only), document format validation or conversion, batch document upload, retry logic for failed extractions beyond basic Celery retries, or a real-time streaming results interface.

Q: What is the minimal version that still delivers the core benefit?
A: A `POST /documents` endpoint that stores ciphertext and queues a Celery task, a Celery worker that forwards ciphertext to the enclave over mTLS, the enclave performing decrypt-extract-encrypt and returning results, a `GET /documents/{id}` endpoint that returns encrypted results, and client-side decryption of those results. This proves the full pipeline works end-to-end with privacy intact.

Q: What are we deferring to future iterations?
A: Tenant-scoped access control (M3), advanced extraction models, document format support beyond plain text, progress tracking within pipeline stages, pipeline monitoring and alerting, and dead letter queue handling for permanently failed documents.

### Technical Constraint FAQs

Q: What existing system capabilities does this depend on?
A: This depends on M0 deliverables (Docker Compose, PostgreSQL, Redis, database schema) and M1 deliverables (enclave key pair generation, mTLS communication, client-side encryption/decryption, attestation flow). The enclave must already be able to decrypt documents and the mTLS channel must be operational.

Q: What new capabilities need to be built?
A: (1) `POST /documents` API endpoint that stores ciphertext and queues a Celery task. (2) Celery task chain: ingest task, forward-to-enclave task, store-results task. (3) Enclave processing endpoint that accepts ciphertext, decrypts, extracts fields via regex, encrypts results, and returns them. (4) `GET /documents/{id}` API endpoint that returns document status and encrypted results. (5) Client SDK functions for uploading encrypted documents and retrieving/decrypting results. (6) Document status tracking in PostgreSQL (PENDING, PROCESSING, COMPLETED, FAILED).

Q: Why Celery chains instead of simple synchronous processing?
A: Celery chains model the production pattern where document processing involves multiple ordered stages (ingest, forward to enclave, store results). This allows each stage to be independently monitored, retried, and scaled. It also mirrors how a real AWS deployment would use SQS-backed Celery for pipeline orchestration.

Q: What is the simplest implementation that delivers the PR outcome?
A: The API endpoint stores the ciphertext in the existing documents table and dispatches a Celery chain of three tasks: (1) mark as PROCESSING, (2) POST the ciphertext to the enclave's mTLS endpoint and receive encrypted results, (3) store encrypted results and mark as COMPLETED. The enclave adds a `/process` endpoint that decrypts, runs regex extraction for parties and dates, encrypts the results, and returns them. The client SDK wraps the upload and retrieval calls.

### Edge Cases & Error States FAQs

Q: What happens if the enclave is unavailable when the Celery worker tries to forward the document?
A: The Celery task fails and is retried according to Celery's retry policy. The document status remains PROCESSING. If retries are exhausted, the status is set to FAILED. The ciphertext remains safely stored in PostgreSQL for manual reprocessing.

Q: What happens if the document contains no extractable parties or dates?
A: The enclave returns an encrypted result with empty extraction fields. This is a valid outcome -- the pipeline succeeds, the document is marked COMPLETED, and the client sees empty results upon decryption. No error is raised.

Q: What happens if the client uploads invalid ciphertext (corrupted or wrong key)?
A: The enclave fails to decrypt and returns an error. The Celery worker marks the document as FAILED. The client sees status=FAILED when polling. No plaintext is exposed because there was none to expose.

Q: What happens if the database is unavailable during result storage?
A: The Celery task for storing results fails and is retried. The encrypted results may need to be re-fetched from the enclave if they were not persisted. In the worst case, the document is marked FAILED and must be resubmitted.

Q: What happens if the client never retrieves the results?
A: The encrypted results remain stored in PostgreSQL indefinitely. No cleanup or expiration is implemented in this milestone. The data is encrypted, so storage of unclaimed results poses no privacy risk.

### Business & Operational FAQs

Q: How will we know if this is successful?
A: Success means: (1) A client can submit an encrypted document and retrieve encrypted results through the full pipeline. (2) The extracted fields (parties, dates) match expected values when the client decrypts the results. (3) At no point during the pipeline does the parent API, database, or Redis contain plaintext. (4) The Celery chain processes documents in the correct order. (5) Document status transitions correctly through PENDING, PROCESSING, COMPLETED.

Q: What could go wrong post-launch?
A: Celery worker crashes mid-pipeline leaving documents stuck in PROCESSING. Enclave restarts causing key pair changes that break in-flight documents. mTLS certificate expiration breaking the parent-to-enclave channel. Redis unavailability blocking task dispatch.

Q: Are there compliance or security considerations?
A: The core security property is maintained: the parent never sees plaintext. All data at rest in PostgreSQL is encrypted. The Celery task queue (Redis) only contains document IDs and metadata, never document content. The enclave processes documents in memory on tmpfs, leaving no persistent trace of plaintext.

---

## Implementation Scope

### Current State -> End State

| Current State | End State |
| --- | --- |
| No API endpoints for document upload or retrieval exist | Client can POST encrypted documents and GET encrypted results via REST API |
| Enclave can decrypt documents but has no processing or extraction capability | Enclave decrypts, extracts parties and dates via regex, encrypts results, and returns them over mTLS |
| No Celery tasks exist for document processing | Celery chain orchestrates the full pipeline: ingest, forward to enclave, store results |
| No document status tracking exists | Documents have status lifecycle: PENDING, PROCESSING, COMPLETED, FAILED |
| Client SDK can encrypt documents but cannot upload or retrieve results | Client SDK handles the full flow: encrypt, upload, poll status, retrieve, and decrypt results |
| Database schema exists but is unused by any pipeline | Database stores encrypted document blobs and encrypted extraction results |

### User Stories

- [ ] As a client, I can POST an encrypted document to the API and get a document ID back (M2.1)
- [ ] As the parent worker, I forward the ciphertext to the enclave over mTLS without ever decrypting it (M2.2)
- [ ] As the enclave, I decrypt the document, extract parties and dates via regex, and return encrypted results (M2.3)
- [ ] As the parent worker, I store the encrypted results in PostgreSQL and update the document status (M2.4)
- [ ] As a client, I can GET a document's encrypted results and decrypt them locally (M2.5)

### Success Criteria

- [ ] Client can upload an encrypted document via `POST /documents` and receive a document ID
- [ ] Celery chain picks up the task and forwards ciphertext to the enclave over mTLS
- [ ] Enclave decrypts the document, extracts at least one party name and one date from a sample legal document
- [ ] Enclave encrypts the extraction results and returns them to the worker
- [ ] Worker stores encrypted results in PostgreSQL and sets document status to COMPLETED
- [ ] Client can retrieve encrypted results via `GET /documents/{id}` and decrypt them locally to see extracted parties and dates
- [ ] Parent API logs and database contain zero plaintext at any point during processing
- [ ] Document status transitions correctly: PENDING -> PROCESSING -> COMPLETED
- [ ] Failed processing (e.g., corrupt ciphertext) results in status=FAILED, not a crash

### Known Compromises

- [ ] Regex extraction only -- delivers parties and dates but not production-quality NLP; acceptable because the focus is pipeline architecture, not extraction accuracy
- [ ] Polling-based status checking -- no webhooks or push notifications; acceptable for MVP simplicity
- [ ] No tenant isolation on API endpoints -- any client can access any document by ID; deferred to M3
- [ ] No document size limits or validation -- acceptable for local MVP with controlled inputs
- [ ] Celery retry policy is basic -- no exponential backoff or dead letter queue; acceptable for MVP

### Open Questions / Needs Research

- [ ] Should the enclave expose a single `/process` endpoint or separate endpoints for decrypt, extract, and encrypt stages?
- [ ] How should the encrypted results be structured? (JSON with encrypted fields vs. single encrypted blob containing all results)
- [ ] What Celery chain structure works best -- three separate tasks or a single task that calls the enclave?
- [ ] How should the Celery worker authenticate to the enclave -- reuse the same mTLS certs as the parent API or separate worker certs?
- [ ] What regex patterns should be used for party and date extraction from legal documents?

---

## Next Steps

When ready to implement:

1. Run `/pipe-research --project legaldocs --ref m2 processing pipeline` to analyze codebase
2. Run `/pipe-plan-implementation --project legaldocs --ref m2 processing pipeline` to create implementation plan
3. Run `/pipe-implement docs/dev/legaldocs/m2-processing-pipeline/implementation-plan.md` to execute the plan
