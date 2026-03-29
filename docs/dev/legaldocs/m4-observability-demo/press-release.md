# Observability & Demo - PR FAQ

> **Milestone**: M4
> **Created**: 2026-03-29
> **Status**: Draft

---

## Press Release

### Observability & Demo

**For** developers evaluating or operating the privacy-preserving legal document processing pipeline
**Who** need to understand what happens inside the pipeline, verify its privacy guarantees, and demonstrate the system end-to-end to stakeholders
**The** Observability & Demo package
**Is a** new set of structured logging, demonstration, and verification capabilities
**That** makes the pipeline fully transparent to operators while proving that privacy guarantees hold under real multi-tenant usage
**Unlike** the current state where pipeline behavior is opaque, privacy claims are unverified, and there is no turnkey way to demonstrate the system working end-to-end
**Our solution** adds structured JSON logging with correlation IDs that trace each document's journey from submission through enclave processing to result storage, a comprehensive demo script that submits sample documents as multiple tenants and displays end-to-end results, and a privacy verification script that programmatically proves the parent process never handles plaintext.

---

Today we are announcing the Observability & Demo package for the legal document processing pipeline, completing the final milestone of the MVP. Developers can now trace any document's complete journey through the system using structured JSON logs with correlation IDs that follow the document from API ingestion, through the Celery task queue, into the enclave for decryption and extraction, and back out to result storage. Every log entry includes the document ID, tenant ID, pipeline stage, and timing information, making it straightforward to diagnose issues or understand system behavior without ever exposing document content.

The package also includes two scripts that turn the pipeline from a collection of services into a demonstrable product. The demo script submits sample legal documents as multiple tenants, polls for completion, retrieves and decrypts the results, and displays the extracted fields -- showing the full pipeline working end-to-end in a single terminal session. The privacy verification script goes further: it inspects the parent process's database records, API responses, Celery task payloads, and log output to confirm that no plaintext document content or extraction results appear anywhere outside the enclave. A separate enclave isolation check verifies that the enclave container cannot reach external networks. Together, these scripts provide concrete, repeatable evidence that the privacy architecture described in the project plan actually works as designed.

---

## FAQ

### Customer Experience

Q: How does a developer access the structured logs?
A: All pipeline components (FastAPI API, Celery worker, enclave server) emit structured JSON logs to stdout. Developers can view them via `docker compose logs` and filter by correlation ID (document ID) to trace a single document's journey. No external logging infrastructure is required.

Q: What does the demo script do step-by-step?
A: The demo script: (1) Starts by verifying all services are healthy. (2) Requests attestation from the enclave and verifies it. (3) Submits sample legal documents as two different tenants (e.g., `acme-corp` and `globex`). (4) Polls `GET /documents/{id}` until processing completes. (5) Retrieves the encrypted results and decrypts them client-side. (6) Displays the extracted fields (parties, dates) for each document. (7) Demonstrates tenant isolation by showing that each tenant can only see their own documents.

Q: What does the privacy verification script prove?
A: The script proves three things: (1) The parent's database contains only ciphertext -- no plaintext document content or extraction results in the `documents` or `extracted_fields` tables. (2) The parent's API responses for document retrieval contain only encrypted blobs. (3) The parent's log output does not contain any plaintext document content. This is verified programmatically by submitting a document with known content, then searching all parent-accessible storage and output for that content.

Q: What does the enclave isolation verification check?
A: It verifies that the enclave container cannot make outbound network requests to external services. The check attempts to reach an external endpoint from within the enclave container and confirms the connection is refused or times out, proving the enclave's `internal` Docker network configuration is enforced.

Q: What information is included in each structured log entry?
A: Each log entry is a JSON object containing: `timestamp`, `level`, `correlation_id` (the document ID), `tenant_id`, `stage` (e.g., `api.ingest`, `worker.forward`, `enclave.decrypt`, `enclave.extract`, `worker.store`), `message`, and `duration_ms` (where applicable). Sensitive fields like document content are never logged.

### Scope & Non-Goals

Q: What is explicitly NOT included in V1?
A: This milestone does NOT include: centralized log aggregation (no ELK/Loki/CloudWatch), distributed tracing (no OpenTelemetry/Jaeger), metrics or dashboards (no Prometheus/Grafana), alerting, log rotation or retention policies, performance benchmarking, load testing, or a web-based UI for viewing pipeline status. The existing `GET /documents/{id}` endpoint serves as the document status check -- no new status endpoint is built.

Q: What is the minimal version that still delivers the core benefit?
A: Structured JSON logs with correlation IDs in all pipeline components, a demo script that submits documents as two tenants and shows results, and a privacy verification script that checks the parent's database and logs for plaintext. The enclave isolation check is small enough to include but could be cut if needed.

Q: What are we deferring to future iterations?
A: OpenTelemetry integration, log shipping to external systems, a dashboard for real-time pipeline monitoring, performance profiling, automated regression testing of privacy guarantees in CI, and a web UI for the demo.

### Technical Constraint FAQs

Q: What existing system capabilities does this depend on?
A: This depends on all prior milestones: M0 (running services and database), M1 (enclave encryption and attestation), M2 (processing pipeline with Celery), and M3 (tenant isolation). The structured logging wraps around existing pipeline stages. The demo and verification scripts exercise the full stack.

Q: What new capabilities need to be built?
A: (1) A structured logging module that produces JSON log entries with correlation IDs, integrated into the FastAPI API, Celery worker, and enclave server. (2) A demo script (`scripts/demo.sh` or `scripts/demo.py`) that exercises the full pipeline with multiple tenants. (3) A privacy verification script (`scripts/verify_privacy.sh` or `scripts/verify_privacy.py`) that proves the parent never sees plaintext. (4) An enclave isolation check (can be part of the verification script or standalone).

Q: What is the simplest implementation that delivers the PR outcome?
A: Use Python's built-in `logging` module with a JSON formatter. Pass the document ID as the correlation ID through all stages (it is already propagated via Celery task arguments and enclave request payloads). The demo and verification scripts can be Python scripts that use the existing client SDK. No new infrastructure or dependencies are needed.

Q: Are there performance considerations?
A: Structured JSON logging adds negligible overhead. The demo and verification scripts are run manually, not in production, so performance is not a concern.

### Edge Cases & Error States FAQs

Q: What happens if a pipeline stage fails -- does the logging still capture it?
A: Yes. Each stage logs both success and failure outcomes. If the enclave is unreachable, the Celery worker logs the error with the correlation ID and the document status is updated to a failed state. The demo script detects and reports this.

Q: What happens if the demo script is run before all services are healthy?
A: The demo script checks service health at the start and exits with a clear error message if any service is not ready, rather than producing confusing partial output.

Q: What if the privacy verification script finds plaintext in an unexpected location?
A: The script exits with a non-zero exit code and prints exactly where the plaintext was found (e.g., which database column, which log line). This is a verification failure that indicates a real privacy bug in the pipeline.

Q: What happens if the enclave isolation check fails?
A: The script reports that the enclave was able to reach an external network, which indicates a Docker network misconfiguration. This would be a critical finding.

### Business & Operational FAQs

Q: How will we know if this is successful?
A: Success means: (1) A developer can trace any document through the pipeline using `docker compose logs` and a document ID. (2) The demo script runs end-to-end without manual intervention and displays extracted fields for multiple tenants. (3) The privacy verification script passes -- confirming no plaintext in the parent's database, API responses, or logs. (4) The enclave isolation check confirms no external network access.

Q: What could go wrong post-launch?
A: A future code change could introduce plaintext logging (the verification script would catch this if run). Log output could become too verbose at scale (not a concern for MVP). The demo script could break if API contracts change.

Q: Does this require changes to support/ops processes?
A: No. This is a developer-facing capability for an MVP. The scripts are run manually during development and demonstration.

Q: Are there compliance or security considerations?
A: The structured logging must never include document content, decrypted text, or extraction results in the parent's logs. Only metadata (document ID, tenant ID, stage, status, timing) should appear. The privacy verification script validates this property.

---

## Implementation Scope

### Current State -> End State

| Current State | End State |
| --- | --- |
| Pipeline components use unstructured or minimal logging with no correlation between stages | All components emit structured JSON logs with correlation IDs (document ID) that trace each document's full journey |
| No way to demonstrate the full pipeline end-to-end without manual, multi-step commands | A single demo script submits documents as multiple tenants and displays end-to-end results |
| Privacy guarantees are claimed in documentation but not programmatically verified | A verification script proves the parent never sees plaintext by inspecting the database, API, and logs |
| Enclave network isolation is configured in Docker Compose but not explicitly tested | An isolation check confirms the enclave cannot reach external networks |
| Developers must read code to understand what happened to a document | Developers can filter logs by document ID to see every stage the document passed through |

### User Stories

- [ ] As a developer, I can see structured JSON logs showing a document's journey through the pipeline with correlation IDs, so that I can trace and debug document processing (M4.2)
- [ ] As a developer, I can run a demo script that submits sample legal documents as different tenants and shows the full end-to-end results, so that I can demonstrate the pipeline to stakeholders (M4.3)
- [ ] As a developer, I can run a privacy verification script that proves the parent process never sees plaintext document content, so that I have concrete evidence of the privacy architecture (M4.1)
- [ ] As a developer, I can run an enclave isolation check that confirms the enclave cannot reach external services, so that I know the network isolation is working (M4.4)
- [ ] As a developer, I can see timing information in the logs for each pipeline stage, so that I can identify slow stages

### Success Criteria

- [ ] All pipeline components (API, Celery worker, enclave) emit structured JSON logs to stdout
- [ ] Every log entry includes `correlation_id` (document ID), `tenant_id`, `stage`, `timestamp`, and `level`
- [ ] No log entry in the parent or worker contains plaintext document content or decrypted extraction results
- [ ] The demo script runs end-to-end: submits documents as 2+ tenants, waits for processing, retrieves and decrypts results, displays extracted fields
- [ ] The demo script demonstrates tenant isolation (each tenant sees only their documents)
- [ ] The privacy verification script checks the database, API responses, and parent logs for plaintext and reports pass/fail
- [ ] The enclave isolation check confirms the enclave cannot make outbound network requests
- [ ] All scripts exit with appropriate exit codes (0 for pass, non-zero for failure)

### Known Compromises

- [ ] No centralized log aggregation -- logs go to stdout only, viewed via `docker compose logs`; acceptable for MVP since no production deployment exists
- [ ] No distributed tracing (OpenTelemetry) -- correlation is done via document ID passed through function arguments, not trace context propagation; acceptable because the pipeline has a simple linear flow
- [ ] No metrics or dashboards -- acceptable for MVP; structured logs provide sufficient observability for development
- [ ] Privacy verification is a manual script, not an automated CI check -- acceptable for MVP; the script can be integrated into CI later
- [ ] Demo script uses polling for document completion rather than webhooks or SSE -- acceptable for MVP simplicity

### Open Questions / Needs Research

- [ ] How are logs currently emitted in the FastAPI API, Celery worker, and enclave server? Need to understand what logging changes are required.
- [ ] Is the document ID already passed through all pipeline stages (API -> Celery task -> enclave request -> result storage), or does it need to be threaded through?
- [ ] What sample legal documents should the demo script use? Can we include small sample files in the repo or should the script generate synthetic text?
- [ ] Should the privacy verification script connect directly to PostgreSQL to inspect the database, or should it use the API only?
- [ ] What is the best way to capture and inspect parent logs programmatically in the verification script?

---

## Next Steps

When ready to implement:

1. Run `/pipe-research --project legaldocs --ref m4 observability demo` to analyze codebase
2. Run `/pipe-plan-implementation --project legaldocs --ref m4 observability demo` to create implementation plan
3. Run `/pipe-implement docs/dev/legaldocs/m4-observability-demo/implementation-plan.md` to execute the plan
