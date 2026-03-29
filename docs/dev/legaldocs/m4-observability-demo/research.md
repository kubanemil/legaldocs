---
date: 2026-03-29
researcher: claude-opus-4-6
branch: main
repository: legaldocs
tags: observability, structured-logging, demo, verification, correlation-id, celery, fastapi, enclave
status: complete
---

# Research: Observability & Demo (M4)

## Research Question

What is the current state of logging, correlation, and demo/verification tooling in the legaldocs pipeline? What patterns and approaches should be used to add structured JSON logging with correlation IDs across FastAPI, Celery, and enclave components, plus demo and privacy verification scripts?

## Summary

The legaldocs pipeline currently has minimal, inconsistent logging across its three main components. The FastAPI API (`parent/api/main.py`) has no application-level logging at all. The Celery worker (`parent/workers/tasks.py`) uses Python's stdlib `logging` module with basic string-formatted messages containing the document ID. The enclave server (`enclave/server.py`) overrides `BaseHTTPRequestHandler.log_message` with a simple `print` statement prefixed by `[enclave]`. None of the components emit structured JSON logs.

The document ID (a UUID) is already threaded through the entire pipeline -- from API submission, through the Celery chain arguments, into the enclave HTTP request, and back to result storage. This means the correlation ID infrastructure is already in place; it just needs to be surfaced in structured log entries. The tenant ID is available at the API layer (via the `X-Tenant-ID` header) and stored on the Document model, but it is not passed through Celery task arguments or forwarded to the enclave.

No demo or verification scripts exist beyond the two setup scripts (`scripts/generate_certs.sh` and `scripts/run_migrations.sh`). The client SDK (`client/client.py`) already provides all the building blocks needed for demo and verification scripts: attestation fetch/verify, document encryption, submission, polling, and result decryption.

## Detailed Findings

### 1. Current Logging State

#### FastAPI API (`parent/api/main.py`)

- **No `import logging`** and no logger instance.
- No application-level log statements in any endpoint.
- Uvicorn provides HTTP access logs by default (method, path, status code), but these are unstructured and contain no document/tenant context.
- Error handling uses `HTTPException` raises, which produce HTTP responses but do not log.
- File: `parent/api/main.py` -- 225 lines, zero log calls.

#### Celery Worker (`parent/workers/tasks.py`)

- Uses `logging.getLogger(__name__)` at line 20.
- Six log calls total:
  - `logger.info("Document %s status set to PROCESSING", document_id)` -- line 69
  - `logger.exception("ingest_document failed for %s", document_id)` -- line 82
  - `logger.warning("Connection error forwarding doc %s to enclave, retrying: %s", ...)` -- line 124
  - `logger.info("Document %s forwarded to enclave successfully", document_id)` -- line 135
  - `logger.exception("forward_to_enclave failed for %s", document_id)` -- line 153
  - `logger.info("Document %s results stored, status COMPLETED", document_id)` -- line 185
- All use `%s` string formatting, not structured/JSON output.
- Document ID is included in every message, but as a positional format argument.
- Tenant ID is **not** included in any log message (it is available on the Document model but not queried or passed).
- No timing/duration information.
- Celery's own logging (task received, task succeeded, etc.) is controlled by the `--loglevel=info` flag in `docker-compose.yml` line 73.

#### Enclave Server (`enclave/server.py`)

- Overrides `BaseHTTPRequestHandler.log_message` at line 172: `print(f"[enclave] {args[0]} {args[1]} {args[2]}")`.
- This captures HTTP method, path, and status code from the base handler.
- No application-level logging in `_handle_process`, `_handle_decrypt`, or `_handle_attestation`.
- Errors in `_handle_process` and `_handle_decrypt` are returned as HTTP 400 JSON responses but not logged.
- Server startup prints two lines: address and enclave ID (lines 183-184).
- No document ID or correlation context in any log output.

#### Celery App Configuration (`parent/workers/celery_app.py`)

- No logging configuration. Celery uses its default logging setup.
- The `--loglevel=info` flag on the worker command controls verbosity.
- No `worker_hijack_root_logger` or `worker_log_format` settings.

### 2. Document ID (Correlation ID) Flow

The document ID is already propagated through the entire pipeline:

1. **API** (`main.py:92-112`): `Document` row created with UUID, `document_id = str(doc.id)` passed to Celery chain as the initial argument.
2. **ingest_document** (`tasks.py:50`): Receives `document_id: str`, returns it for the next task.
3. **forward_to_enclave** (`tasks.py:94`): Receives `document_id: str`, reads encrypted content from DB, POSTs to enclave `/process`. Returns `{"document_id": document_id, "encrypted_result_b64": ...}`.
4. **Enclave /process** (`server.py:124-153`): Receives raw encrypted bytes via HTTP POST. **Does not receive the document ID** -- the enclave only sees the encrypted blob via `Content-Type: application/octet-stream`. The document ID is not in headers or body.
5. **store_results** (`tasks.py:159`): Receives the dict with `document_id` and `encrypted_result_b64`, stores in DB.

Key observation: The enclave does not know the document ID. To add correlation logging in the enclave, the document ID would need to be passed as an HTTP header (e.g., `X-Correlation-ID`) in the `forward_to_enclave` task's request to the enclave.

### 3. Tenant ID Flow

- The tenant ID arrives via `X-Tenant-ID` HTTP header, validated in `parent/api/dependencies.py:get_tenant_id`.
- Stored on the `Document` model as `tenant_id` column (`parent/models/models.py:27`).
- **Not passed** to Celery tasks -- the tasks receive only `document_id` and load the document from DB. The tenant_id is available on the loaded `doc` object but never logged.
- **Not forwarded** to the enclave in any form.

### 4. Existing Scripts

- `scripts/generate_certs.sh` -- generates mTLS certificates for CA, parent, and enclave.
- `scripts/run_migrations.sh` -- runs Alembic migrations.
- No demo script exists.
- No privacy verification script exists.

### 5. Client SDK Capabilities

The `LegalDocsClient` class in `client/client.py` provides:

- `get_attestation()` -- fetch attestation from API (line 56)
- `verify_attestation(attestation, ca_cert_path)` -- verify signature chain, return public key PEM (line 67)
- `encrypt_document(plaintext, public_key_pem)` -- hybrid RSA/AES envelope encryption (line 134)
- `submit_document(ciphertext, filename)` -- POST encrypted doc (line 176)
- `get_document_status(document_id)` -- GET status (line 207)
- `list_documents(status)` -- GET list filtered by tenant (line 224)
- `get_document_results(document_id, poll_interval, max_retries)` -- poll until COMPLETED, then decrypt (line 243)
- `decrypt_results(encrypted_result_b64)` -- decrypt via /decrypt proxy (line 291)

All methods needed for both demo and verification scripts are already available.

### 6. Docker Compose Networking

- `external-net`: bridge network connecting postgres, redis, api, worker.
- `enclave-net`: **internal** bridge network (line 119: `internal: true`) connecting api, worker, enclave.
- The enclave service is **only** on `enclave-net` and has no connection to `external-net`.
- Enclave container is `read_only: true` with `tmpfs: [/tmp]` (lines 98-100).
- This means the enclave isolation check can verify that `docker exec` into the enclave cannot reach external hosts.

### 7. Database Schema

- `documents` table: `id` (UUID PK), `tenant_id`, `filename`, `content_type`, `encrypted_content` (LargeBinary), `encrypted_result` (LargeBinary), `status`, `error_message`, `created_at`, `updated_at`.
- `extracted_fields` table: `id`, `document_id` (FK), `tenant_id`, `field_name`, `encrypted_value` (LargeBinary).
- All content columns store binary (encrypted) data. No plaintext columns exist.
- The `error_message` column (Text) could potentially contain plaintext fragments if an exception message includes document content -- this should be checked by the verification script.

## Research: Structured Logging Approaches

### Python stdlib JSON Logging

The simplest approach that requires no new dependencies:

```python
import json
import logging
import sys
from datetime import datetime, timezone

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Add extra fields if present
        for key in ("correlation_id", "tenant_id", "stage", "duration_ms"):
            if hasattr(record, key):
                log_entry[key] = getattr(record, key)
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)
```

This can be applied to both FastAPI and Celery by configuring handlers at startup.

### structlog

`structlog` provides a more ergonomic API for structured logging with context binding:

```python
import structlog
logger = structlog.get_logger()
logger.info("document.ingested", correlation_id=doc_id, tenant_id=tenant_id, stage="api.ingest")
```

Pros: cleaner API, context binding (bind once, log everywhere), processors for adding timestamps/levels. Cons: adds a dependency.

Given the press release specifies "Python's built-in logging module with a JSON formatter" (FAQ section), **stdlib with a custom JSON formatter is the intended approach**.

### Celery Logging Integration

Celery hijacks the root logger by default. To integrate custom JSON logging:

1. Use Celery's `after_setup_logger` signal to attach the JSON formatter:

```python
from celery.signals import after_setup_logger

@after_setup_logger.connect
def setup_loggers(logger, **kwargs):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    logger.handlers = [handler]
```

2. Alternatively, set `worker_hijack_root_logger = False` in Celery config and configure logging independently.

3. For correlation context in Celery tasks, the document ID is already a task argument. It can be passed to log calls via `logger.info("msg", extra={"correlation_id": document_id})` or using a `logging.LoggerAdapter`.

### Correlation ID Propagation to Enclave

The enclave currently receives only raw bytes. To propagate the correlation ID:

- Add `X-Correlation-ID` header in the `forward_to_enclave` task's HTTP request to the enclave.
- Read it in the `EnclaveHandler` and include it in log output.
- This is a minimal change: one header addition in `tasks.py` and one header read in `server.py`.

### Demo Script Patterns

The demo script should be a Python script (not bash) to leverage the existing `LegalDocsClient` SDK. Pattern:

1. Health check all services via API `/health` and enclave health (proxied or direct).
2. Request and verify attestation.
3. Encrypt and submit sample documents as 2+ tenants.
4. Poll for completion.
5. Retrieve and decrypt results.
6. Display extracted fields.
7. Demonstrate tenant isolation (list docs for each tenant, verify no cross-tenant leakage).

Sample legal documents can be inline strings in the script -- no need for external files for MVP.

### Privacy Verification Script Patterns

The verification script should:

1. Submit a document with **known unique content** (e.g., a UUID marker string).
2. Wait for processing to complete.
3. Connect to PostgreSQL directly and search `documents.encrypted_content`, `documents.encrypted_result`, `documents.error_message`, and `extracted_fields.encrypted_value` for the marker string.
4. Search parent/worker logs (captured via `docker compose logs api worker`) for the marker string.
5. Verify API responses contain only encrypted data.
6. Report pass/fail for each check.

Direct PostgreSQL access is the most reliable way to inspect stored data. The `DATABASE_URL` is already a known value (`postgresql://postgres:postgres@localhost:5432/legaldocs` when port-mapped, or the Docker internal URL).

### Enclave Isolation Check

Use `docker compose exec enclave` to attempt outbound connections:

```bash
docker compose exec enclave python -c "
import urllib.request
try:
    urllib.request.urlopen('http://1.1.1.1', timeout=3)
    print('FAIL: enclave can reach external network')
except:
    print('PASS: enclave cannot reach external network')
"
```

This verifies the `internal: true` network configuration.

## Code References

- `parent/api/main.py` -- FastAPI app, no logging, document submission at lines 72-112
- `parent/api/dependencies.py` -- tenant ID extraction from header, lines 14-33
- `parent/workers/tasks.py:20` -- `logger = logging.getLogger(__name__)`, only logger in codebase
- `parent/workers/tasks.py:49-85` -- `ingest_document` task, logs at lines 69, 82
- `parent/workers/tasks.py:88-156` -- `forward_to_enclave` task, logs at lines 124, 135, 153
- `parent/workers/tasks.py:159-199` -- `store_results` task, logs at line 185
- `parent/workers/celery_app.py` -- Celery config, no logging setup, 27 lines
- `enclave/server.py:172-174` -- `log_message` override, print-based logging
- `enclave/server.py:124-153` -- `/process` handler, no logging, no correlation ID
- `enclave/extractor.py:104-131` -- `extract_fields()` returns `{"parties": [...], "dates": [...]}`
- `client/client.py` -- full SDK, all methods needed for demo/verification
- `parent/models/models.py:25-57` -- Document model, `encrypted_content`, `encrypted_result`, `error_message` columns
- `docker-compose.yml:114-119` -- network definitions, `enclave-net` is `internal: true`
- `docker-compose.yml:73` -- worker command with `--loglevel=info`
- `scripts/generate_certs.sh` -- cert generation
- `scripts/run_migrations.sh` -- migration runner

## Architecture Notes

The pipeline has a clear three-stage flow: API (FastAPI) -> Worker (Celery) -> Enclave (stdlib HTTP server). The API and Worker share the same Docker image (parent Dockerfile) but run different commands. The enclave is a separate image on an internal-only network.

For structured logging, each component needs its own setup:
- **FastAPI**: Configure via `logging.config` at app startup or in a lifespan handler. Uvicorn's access logs can be reformatted or supplemented with application-level logs.
- **Celery**: Use signals (`after_setup_logger` or `setup_logging`) to attach the JSON formatter. Task-level logging already uses stdlib.
- **Enclave**: Replace the `print`-based `log_message` override and add application-level logging in handlers. Since the enclave uses stdlib's `http.server`, logging can use stdlib directly.

The correlation ID (document ID) flows through Celery task arguments but does not reach the enclave. Adding an `X-Correlation-ID` header to the enclave request in `forward_to_enclave` is the minimal change needed.

The tenant ID is available on the Document model but not surfaced in any logging. It can be read from the DB in Celery tasks (it is on the loaded `doc` object) and included in log entries.

## Open Questions

1. **Log stage naming convention**: The press release mentions stages like `api.ingest`, `worker.forward`, `enclave.decrypt`, `enclave.extract`, `worker.store`. Should these be exactly these strings, or can they follow the codebase's existing naming?

2. **Enclave logging format**: The enclave uses a plain `print` override. Should it switch fully to Python's `logging` module with the same JSON formatter, or is a simpler JSON print approach acceptable given the enclave's minimal codebase?

3. **Demo script sample documents**: The press release mentions two tenants (`acme-corp` and `globex`). Should the sample documents be hardcoded strings in the script, or should they be loaded from a `samples/` directory?

4. **Privacy verification DB access**: Should the verification script connect to PostgreSQL directly (requires `psycopg2` or `sqlalchemy` in the client environment) or should it query via the API only? Direct DB access is more thorough (can inspect raw binary columns) but adds a dependency.

5. **Verification of Celery task payloads**: The press release FAQ mentions checking "Celery task payloads" for plaintext. Redis stores serialized task arguments. Should the verification script inspect Redis directly, or is checking the DB and logs sufficient for MVP?

---

## Next Steps

If you are satisfied with this research, proceed to planning:

/pipe-plan-implementation --project legaldocs --ref m4 --research docs/dev/legaldocs/m4-observability-demo/research.md observability demo

If you need more research:

- Ask follow-up questions and I will append to this document
- Or request a new research topic with /pipe-research --project legaldocs --ref m4 [new-topic]
