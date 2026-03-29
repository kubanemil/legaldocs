---
date: 2026-03-29
author: claude-opus-4-6
status: draft
branch: main
related_research: docs/dev/legaldocs/m4-observability-demo/research.md
---

# Plan: Observability & Demo (M4)

## Overview

Add structured JSON logging with correlation IDs across all pipeline components (FastAPI API, Celery worker, enclave server), then create two scripts: a demo script that exercises the full multi-tenant pipeline end-to-end, and a privacy verification script that programmatically proves the parent process never handles plaintext. This completes the final MVP milestone.

The implementation requires no new infrastructure or external dependencies. It uses Python's built-in `logging` module with a custom JSON formatter, and the existing `LegalDocsClient` SDK for the scripts.

## Architecture Decisions

- **stdlib logging with JSON formatter** because the press release specifies no new dependencies and the pipeline is simple enough that stdlib suffices
- **Document ID as correlation ID** because it already flows through all pipeline stages (API -> Celery args -> enclave); no new propagation mechanism needed
- **X-Correlation-ID header to enclave** because the enclave currently receives only raw bytes and has no document context for logging
- **Shared logging module in `parent/logging_config.py`** because both the API and Celery worker share the same Docker image and codebase
- **Separate simple JSON logging in enclave** because the enclave has its own codebase and Dockerfile; a lightweight standalone approach avoids cross-image dependencies
- **Python demo/verification scripts using LegalDocsClient** because all SDK methods already exist; no bash scripting complexity needed
- **Direct PostgreSQL access in verification script** because inspecting raw binary columns is the only way to definitively prove no plaintext exists in the DB
- **Inline sample documents** because the demo only needs small text strings, not real legal files

## Prerequisites

- [ ] All services running via `docker compose up` (M0-M3 complete)
- [ ] Certificates generated via `scripts/generate_certs.sh`
- [ ] Migrations run via `scripts/run_migrations.sh`

---

## Phase 1: Structured Logging

### Goal

Add structured JSON logging with correlation IDs and timing to all three pipeline components so that every document's journey is traceable via `docker compose logs | grep <document_id>`.

### Files to Modify

- `parent/logging_config.py` - **New file**: shared JSON formatter and logger setup for parent components
- `parent/api/main.py` - Add structured logging to all endpoints with correlation_id, tenant_id, stage, duration_ms
- `parent/workers/celery_app.py` - Configure Celery to use JSON formatter via `after_setup_logger` signal
- `parent/workers/tasks.py` - Replace `%s` format logging with structured JSON logging including tenant_id, stage, duration_ms
- `enclave/server.py` - Replace `print`-based logging with JSON-formatted logging, read `X-Correlation-ID` header

### Module Design

**`parent/logging_config.py`** - Shared JSON log formatter and configuration for parent-side components.

Key functions/classes:

- `JSONFormatter(logging.Formatter)` - Formats log records as JSON with fields: `timestamp`, `level`, `logger`, `message`, `correlation_id`, `tenant_id`, `stage`, `duration_ms`, `exception`
- `setup_logging(service_name: str) -> logging.Logger` - Configures root logger with JSONFormatter on stdout, returns named logger
- `LogContext(correlation_id, tenant_id, stage)` - Context manager or helper for timing + structured extra fields

### Data Structures

```
JSON log entry {
  timestamp: ISO 8601 string
  level: INFO | WARNING | ERROR
  logger: string (module name)
  message: string
  correlation_id: string (document UUID) | null
  tenant_id: string | null
  stage: string (e.g. "api.submit", "worker.ingest", "worker.forward", "worker.store")
  duration_ms: float | null
  service: "api" | "worker" | "enclave"
  exception: string | null
}
```

### Integration

**API (`main.py`)**: Call `setup_logging("api")` at module level. Add log calls in `submit_document` (stage `api.submit`), `get_document_status` (stage `api.get_status`), `get_attestation` (stage `api.attestation`), `decrypt_result` (stage `api.decrypt`). Include `correlation_id=document_id` and `tenant_id` where available.

**Celery (`celery_app.py`)**: Use `after_setup_logger` signal to replace Celery's default formatter with `JSONFormatter`. This ensures all Celery-emitted logs (task received, succeeded, failed) are also JSON.

**Celery tasks (`tasks.py`)**: Replace existing `logger.info("Document %s ...", document_id)` calls with `logger.info("message", extra={"correlation_id": document_id, "tenant_id": doc.tenant_id, "stage": "worker.ingest", "duration_ms": elapsed})`. Read `tenant_id` from the loaded `doc` object (already available in each task).

**Enclave (`server.py`)**: Add standalone JSON logging (no shared module -- different Docker image). Read `X-Correlation-ID` from request headers in `_handle_process` and `_handle_decrypt`. Log stages: `enclave.process`, `enclave.decrypt`, `enclave.attestation`.

**Forward correlation to enclave**: In `tasks.py` `forward_to_enclave`, add `"X-Correlation-ID": document_id` to the headers dict in the `client.post("/process", ...)` call.

### Success Criteria

**Automated Verification:**

```bash
docker compose up -d
# Submit a document via client SDK
docker compose logs api worker enclave 2>&1 | python -c "
import sys, json
for line in sys.stdin:
    try:
        entry = json.loads(line.split('|',1)[-1].strip())
        if 'correlation_id' in entry:
            print(f'{entry[\"stage\"]}: {entry[\"message\"]}')
    except: pass
"
```

**Manual Verification:**

- [ ] `docker compose logs api` emits JSON lines with `correlation_id` and `tenant_id` for document submissions
- [ ] `docker compose logs worker` emits JSON lines with `correlation_id`, `tenant_id`, `stage`, and `duration_ms` for each pipeline task
- [ ] `docker compose logs enclave` emits JSON lines with `correlation_id` and `stage` for process/decrypt requests
- [ ] No log line in any component contains plaintext document content
- [ ] Filtering logs by a document ID shows the complete journey: api.submit -> worker.ingest -> worker.forward -> enclave.process -> worker.store

---

## Phase 2: Demo & Verification Scripts

### Goal

Create two Python scripts that make the pipeline demonstrable and provably private: `scripts/demo.py` (end-to-end multi-tenant demo) and `scripts/verify_privacy.py` (programmatic privacy proof + enclave isolation check).

### Files to Modify

- `scripts/demo.py` - **New file**: end-to-end demo exercising full pipeline with two tenants
- `scripts/verify_privacy.py` - **New file**: privacy verification + enclave isolation check
- `client/requirements.txt` - Add `psycopg2-binary` (needed by verify_privacy.py for direct DB inspection)

### Module Design

**`scripts/demo.py`** - Self-contained demo script that imports `LegalDocsClient`.

Key flow:

- `check_health(api_url) -> bool` - Verify API and enclave are reachable
- `run_demo(api_url, ca_cert_path)` - Main demo flow:
  1. Health check
  2. Fetch and verify attestation
  3. Create two tenant clients (`acme-corp`, `globex`)
  4. Submit 1-2 sample legal documents per tenant (inline text with party names and dates)
  5. Poll for completion via `get_document_results()`
  6. Display extracted fields (parties, dates) per document
  7. Demonstrate tenant isolation: list docs for each tenant, assert no cross-tenant visibility
  8. Print summary

**`scripts/verify_privacy.py`** - Privacy verification script.

Key flow:

- `verify_database(db_url, marker_string) -> bool` - Connect to PostgreSQL, search `documents.encrypted_content`, `documents.encrypted_result`, `documents.error_message`, `extracted_fields.encrypted_value` for the marker
- `verify_api_responses(api_url, tenant_id, document_id, marker_string) -> bool` - GET document status, search response body for marker
- `verify_logs(marker_string) -> bool` - Capture `docker compose logs api worker` output, search for marker
- `verify_enclave_isolation() -> bool` - Run `docker compose exec enclave python -c "..."` to attempt outbound connection
- `run_verification(api_url, db_url, ca_cert_path)` - Main flow:
  1. Generate a unique marker string (UUID)
  2. Create a document containing the marker
  3. Submit, wait for completion
  4. Run all four verification checks
  5. Report pass/fail per check with clear output
  6. Exit 0 if all pass, non-zero otherwise

### Data Structures

```
Demo sample document {
  text: string (inline legal text with party names and dates)
  tenant_id: string ("acme-corp" or "globex")
  filename: string
}

Verification result {
  check_name: string
  passed: bool
  detail: string (what was checked, what was found if failed)
}
```

### Integration

Both scripts import from `client/client.py` (the existing `LegalDocsClient`). They are run from the host machine (not inside Docker), connecting to `http://localhost:8000`.

The verification script additionally connects directly to PostgreSQL at `postgresql://postgres:postgres@localhost:5432/legaldocs` (the port-mapped database) and uses `subprocess` to run `docker compose exec` for the enclave isolation check.

Default values (API URL, DB URL, CA cert path) should be configurable via CLI arguments with sensible defaults matching docker-compose.yml.

### Success Criteria

**Manual Verification:**

- [ ] `python scripts/demo.py` runs end-to-end without errors, submits docs as two tenants, displays extracted parties and dates
- [ ] Demo script output shows tenant isolation (each tenant only sees their own documents)
- [ ] `python scripts/verify_privacy.py` passes all checks: database, API responses, logs, enclave isolation
- [ ] Verification script exits 0 on success, non-zero on failure
- [ ] Both scripts print clear, readable output suitable for stakeholder demonstration

---

## Phase 3: Integration Verification

### Goal

Run both scripts against the live pipeline and verify that structured logging produces traceable, correlation-ID-linked output. Ensure all M4 user stories and success criteria from the press release are satisfied.

### Success Criteria

**Automated Verification:**

```bash
# Start services
docker compose up -d --build

# Wait for healthy
sleep 10

# Run demo
python scripts/demo.py

# Run privacy verification
python scripts/verify_privacy.py

# Verify structured logs have correlation IDs
docker compose logs api worker enclave 2>&1 | python -c "
import sys, json
ids = set()
for line in sys.stdin:
    try:
        entry = json.loads(line.split('|',1)[-1].strip())
        if entry.get('correlation_id'):
            ids.add(entry['correlation_id'])
    except: pass
print(f'Found {len(ids)} unique correlation IDs in logs')
assert len(ids) > 0, 'No correlation IDs found'
"
```

**Comprehensive E2E Tests (from press release user stories):**

- [ ] M4.2: Structured JSON logs show a document's journey with correlation IDs (filter `docker compose logs` by document ID, see all stages)
- [ ] M4.3: Demo script submits sample docs as different tenants, shows end-to-end results (run `scripts/demo.py`)
- [ ] M4.1: Privacy verification proves parent never sees plaintext (run `scripts/verify_privacy.py`, all checks pass)
- [ ] M4.4: Enclave isolation check confirms no external network access (included in `verify_privacy.py`)
- [ ] Every log entry includes `correlation_id`, `tenant_id`, `stage`, `timestamp`, `level`
- [ ] No parent/worker log contains plaintext document content
- [ ] Demo shows tenant isolation (each tenant sees only their docs)
- [ ] Both scripts exit with appropriate exit codes

**Manual Verification:**

- [ ] Walk through demo script output and confirm extracted fields are displayed correctly
- [ ] Verify timing information (`duration_ms`) appears in worker logs for pipeline stages
- [ ] Confirm enclave logs include correlation ID received via `X-Correlation-ID` header

---

## Rollback Plan

All changes are additive:
- Structured logging: revert `parent/logging_config.py` creation and undo modifications to `main.py`, `tasks.py`, `celery_app.py`, `server.py`
- Scripts: delete `scripts/demo.py` and `scripts/verify_privacy.py`
- No database schema changes, no new services, no Docker Compose changes

## Open Questions

- **Log verbosity**: Should Uvicorn's default access logs be suppressed or reformatted to JSON? For MVP, leaving them as-is alongside application logs is acceptable.
- **Celery internal logs**: Celery emits its own task lifecycle logs (received, succeeded). The `after_setup_logger` signal reformats these to JSON, but their content is controlled by Celery. Acceptable for MVP.
- **Port mapping for PostgreSQL**: The verification script needs direct DB access. If PostgreSQL port 5432 is not mapped in docker-compose.yml, a port mapping needs to be added. Current compose file does not map the postgres port -- this will need to be added (or the script can use `docker compose exec postgres psql`).

---

## Next Steps

If you approve this plan, proceed to implementation:

/pipe-implement docs/dev/legaldocs/m4-observability-demo/implementation-plan.md

If you want changes:

- Tell me what to revise and I'll update the plan
- Ask questions about specific phases or decisions
