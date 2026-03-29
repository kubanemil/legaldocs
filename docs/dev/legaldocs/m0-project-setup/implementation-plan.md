---
date: 2026-03-29
author: claude-opus-4-6
status: draft
branch: main
related_research: docs/dev/legaldocs/m0-project-setup/research.md
---

# Plan: M0 Project Setup

## Overview

This plan establishes the foundational infrastructure for the LegalDocs privacy-preserving document processing pipeline. By the end of M0, a developer can run `docker compose up` and have all four services (FastAPI API, Celery worker, PostgreSQL, Redis, and a network-isolated enclave container) running with the database schema applied, mTLS certificates in place, and health check endpoints confirming everything works.

The implementation is split into four sequential phases: project scaffolding, Docker infrastructure, database schema, and integration verification.

## Architecture Decisions

- **Python 3.12** as the base image: stable, good performance, broad dependency support
- **Enclave server uses Python stdlib** (`ssl` + `http.server`): minimal dependencies for the isolated container, no framework needed for a health check
- **Alembic migrations via a separate init container** (not entrypoint script): cleaner separation of concerns, runs once and exits
- **Health check format**: JSON `{"status": "healthy", "service": "<name>"}` on all services
- **`alembic.ini` at project root**: standard convention, allows running `alembic` commands from project root
- **Single Dockerfile for parent** serving both API and worker (different `command`): reduces image duplication
- **2048-bit RSA keys** for mTLS certs: faster generation, sufficient for local dev simulation
- **Environment variables for all config** (DB URL, Redis URL, cert paths): no hardcoded values in application code

## Prerequisites

- [ ] Docker and Docker Compose v2+ installed locally
- [ ] `openssl` CLI available for certificate generation
- [ ] Python 3.12+ available locally (for running alembic outside Docker if needed)

---

## Phase 1: Project Scaffolding

### Goal

Create the complete directory structure, all `__init__.py` files, `requirements.txt` files, `.gitignore`, `.env.example`, and placeholder Python modules so that subsequent phases have a skeleton to build on.

### Files to Create

- `.gitignore` - Standard Python + project-specific ignores (certs/, .env, __pycache__, etc.)
- `.env.example` - Template with all environment variables (DATABASE_URL, REDIS_URL, cert paths)
- `parent/__init__.py` - Empty package init
- `parent/requirements.txt` - FastAPI, uvicorn, celery, redis, sqlalchemy, psycopg2-binary, alembic
- `parent/api/__init__.py` - Empty
- `parent/api/main.py` - FastAPI app with `/health` endpoint returning `{"status": "healthy", "service": "api"}`
- `parent/api/schemas.py` - Pydantic models: `HealthResponse`
- `parent/api/dependencies.py` - DB session dependency, config loading from env vars
- `parent/db.py` - SQLAlchemy engine and session factory, reading DATABASE_URL from env
- `parent/workers/__init__.py` - Empty
- `parent/workers/celery_app.py` - Celery instance configured via env vars (broker, backend, serialization, timeouts)
- `parent/workers/tasks.py` - Empty placeholder with docstring
- `parent/models/__init__.py` - Empty
- `parent/models/models.py` - SQLAlchemy ORM models for `documents` and `extracted_fields` tables
- `enclave/requirements.txt` - `cryptography` only (for future M1 use; M0 needs no deps beyond stdlib)
- `enclave/server.py` - mTLS server using `ssl` + `http.server` with `/health` endpoint
- `enclave/extractor.py` - Placeholder with docstring (M2)
- `enclave/crypto.py` - Placeholder with docstring (M1)
- `client/requirements.txt` - Placeholder (M1)
- `client/client.py` - Placeholder with docstring (M1)
- `scripts/generate_certs.sh` - mTLS certificate generation script (CA, enclave server cert, parent client cert)
- `scripts/run_migrations.sh` - Wrapper to run `alembic upgrade head`

### Module Design

**`parent.api.main`** - FastAPI application entry point

Key endpoints:
- `GET /health -> HealthResponse` - Returns API health status, checks DB and Redis connectivity

**`parent.db`** - Database connection management

Key functions:
- `get_engine() -> Engine` - Creates SQLAlchemy engine from DATABASE_URL env var
- `get_session() -> Generator[Session]` - Yields a database session (FastAPI dependency)

**`parent.workers.celery_app`** - Celery application instance

Key config:
- Broker: `REDIS_URL` env var (default `redis://redis:6379/0`)
- Backend: `REDIS_URL` env var db 1 (default `redis://redis:6379/1`)
- JSON serialization, UTC timezone, 5 min task time limit

**`parent.models.models`** - ORM table definitions

Key models:
- `Document` - id (UUID), tenant_id, filename, content_type, encrypted_content (LargeBinary), encrypted_result (LargeBinary, nullable), status (String, default PENDING), error_message, created_at, updated_at
- `ExtractedField` - id (UUID), document_id (FK), tenant_id, field_name, encrypted_value (LargeBinary), created_at

**`enclave/server.py`** - Stdlib mTLS HTTP server

Key functions:
- `create_ssl_context() -> ssl.SSLContext` - Loads server cert/key, requires client cert, verifies against CA
- `run_server(host, port)` - Starts HTTPS server with mTLS on port 8443
- `GET /health -> {"status": "healthy", "service": "enclave"}` - Health check handler

**`scripts/generate_certs.sh`** - Certificate generation

Produces in `certs/` directory:
- `ca.key`, `ca.crt` - Self-signed CA (2048-bit, 3650 days)
- `enclave.key`, `enclave.crt` - Server cert with SAN=DNS:enclave,DNS:localhost
- `parent.key`, `parent.crt` - Client cert for parent

### Data Structures

```
Document {
  id: UUID (PK, default gen_random_uuid)
  tenant_id: String(64), NOT NULL, indexed
  filename: String(255), nullable
  content_type: String(128), nullable
  encrypted_content: LargeBinary, NOT NULL
  encrypted_result: LargeBinary, nullable
  status: String(20), NOT NULL, default "PENDING", CHECK IN (PENDING, PROCESSING, COMPLETED, FAILED)
  error_message: Text, nullable
  created_at: DateTime(tz), NOT NULL, default now
  updated_at: DateTime(tz), NOT NULL, default now, onupdate now
}

ExtractedField {
  id: UUID (PK, default gen_random_uuid)
  document_id: UUID (FK -> documents.id, ON DELETE CASCADE)
  tenant_id: String(64), NOT NULL, indexed
  field_name: String(128), NOT NULL
  encrypted_value: LargeBinary, NOT NULL
  created_at: DateTime(tz), NOT NULL, default now
}

Indexes:
  - documents(tenant_id)
  - documents(status)
  - documents(tenant_id, status)
  - extracted_fields(document_id)
  - extracted_fields(tenant_id)
```

### Success Criteria

**Automated Verification:**
- `python -c "from parent.api.main import app"` succeeds
- `python -c "from parent.models.models import Document, ExtractedField"` succeeds
- `python -c "from parent.workers.celery_app import celery_app"` succeeds
- `bash scripts/generate_certs.sh` produces 6 files in `certs/`

**Manual Verification:**
- [ ] All `__init__.py` files exist and packages are importable
- [ ] `.gitignore` covers certs/, .env, __pycache__, *.pyc, .venv/
- [ ] `scripts/generate_certs.sh` is executable and idempotent

---

## Phase 2: Docker Infrastructure

### Goal

Create Dockerfiles for parent and enclave, Docker Compose with two networks (external-net, enclave-net with `internal: true`), all service definitions with health checks, volume mounts for certificates, and proper dependency ordering.

### Files to Create

- `parent/Dockerfile` - Python 3.12-slim, copies parent/ and alembic/, installs requirements
- `enclave/Dockerfile` - Python 3.12-slim, copies enclave/, read-only filesystem, tmpfs /tmp
- `docker-compose.yml` - Full stack: postgres, redis, api, worker, migrate, enclave

### Module Design

**`parent/Dockerfile`** - Multi-purpose image for API, worker, and migration runner

Build steps:
- Base: `python:3.12-slim`
- Install system deps for psycopg2 if needed (or use psycopg2-binary)
- Copy `parent/requirements.txt`, install deps
- Copy `parent/` and `alembic/` and `alembic.ini`
- Working directory: `/app`
- No default CMD (set per service in docker-compose)

**`enclave/Dockerfile`** - Minimal isolated server image

Build steps:
- Base: `python:3.12-slim`
- Copy `enclave/requirements.txt`, install deps
- Copy `enclave/`
- Working directory: `/app`
- CMD: `python -m enclave.server` (or `python enclave/server.py`)
- Note: `read_only: true` and `tmpfs` set in docker-compose, not Dockerfile

**`docker-compose.yml`** - Service orchestration

Services:
- `postgres` - `postgres:16-alpine`, external-net, healthcheck via pg_isready, creates `legaldocs` database
- `redis` - `redis:7-alpine`, external-net, healthcheck via redis-cli ping
- `migrate` - parent image, runs `alembic upgrade head`, depends on postgres healthy, exits after completion
- `api` - parent image, uvicorn on port 8000, external-net + enclave-net, depends on postgres + redis + migrate
- `worker` - parent image, celery worker, external-net + enclave-net, depends on redis + migrate
- `enclave` - enclave image, enclave-net only, port 8443 (internal only), read_only, tmpfs /tmp, healthcheck via curl to /health

Networks:
- `external-net` - bridge (default)
- `enclave-net` - bridge, `internal: true`

Volumes/mounts:
- Certs mounted read-only into api, worker, and enclave containers
- Environment variables for DATABASE_URL, REDIS_URL, cert paths

### Integration

- The `migrate` service uses the same image as `api`/`worker` but runs alembic and exits
- `api` and `worker` share the parent image, differ only in command
- `enclave` is on enclave-net only; `api` and `worker` bridge both networks
- Health checks on postgres and redis gate service startup via `depends_on` conditions

### Success Criteria

**Automated Verification:**
- `docker compose config` validates without errors
- `docker compose build` succeeds for all images

**Manual Verification:**
- [ ] `docker compose up -d` starts all services
- [ ] `docker compose ps` shows all services healthy
- [ ] Enclave container cannot reach external network: `docker compose exec enclave ping -c1 google.com` fails
- [ ] Enclave container cannot reach postgres: `docker compose exec enclave ping -c1 postgres` fails
- [ ] API container can reach enclave: `docker compose exec api curl -k https://enclave:8443/health` succeeds

---

## Phase 3: Database Schema via Alembic

### Goal

Set up Alembic configuration and create the initial migration that creates the `documents` and `extracted_fields` tables with all columns, constraints, and indexes as defined in Phase 1.

### Files to Create

- `alembic.ini` - Alembic config at project root, sqlalchemy.url from env
- `alembic/env.py` - Migration environment, reads DATABASE_URL from env, imports target_metadata from models
- `alembic/script.py.mako` - Standard migration template
- `alembic/versions/001_initial_schema.py` - Creates documents and extracted_fields tables

### Module Design

**`alembic/env.py`** - Migration runner configuration

Key behavior:
- `get_url() -> str` - Reads DATABASE_URL from environment, falls back to localhost default
- Imports `Base.metadata` from `parent.models.models` as `target_metadata`
- Supports both online and offline migration modes

**`alembic/versions/001_initial_schema.py`** - Initial migration

Operations:
- `upgrade()` - Creates `documents` table, creates `extracted_fields` table, creates all indexes
- `downgrade()` - Drops both tables

### Integration

- The `migrate` Docker Compose service runs `alembic upgrade head` which executes this migration
- `parent.models.models` defines the same schema as ORM models, so Alembic autogenerate would produce equivalent SQL
- The migration is written manually (not autogenerated) for explicitness

### Success Criteria

**Automated Verification:**
- `docker compose up migrate` exits with code 0
- `docker compose exec postgres psql -U postgres -d legaldocs -c "\dt"` shows both tables

**Manual Verification:**
- [ ] `documents` table has all columns with correct types
- [ ] `extracted_fields` table has foreign key to documents
- [ ] All indexes exist (tenant_id, status, tenant+status, document_id)
- [ ] CHECK constraint on documents.status works (insert with invalid status fails)

---

## Phase 4: Integration Verification

### Goal

Verify all components work together end-to-end: all services start, health checks pass, database schema is applied, mTLS communication works between parent and enclave, and network isolation is enforced.

### Files to Modify

- `parent/api/main.py` - Ensure health check verifies DB and Redis connectivity (not just returns 200)
- `enclave/server.py` - Ensure health check confirms mTLS is active (client cert was presented)

### Success Criteria

**Automated Verification:**

Full startup and verification sequence:
1. `scripts/generate_certs.sh` - generates certificates
2. `docker compose build` - builds all images
3. `docker compose up -d` - starts all services
4. `docker compose ps` - all services healthy
5. `curl http://localhost:8000/health` - API health check returns healthy JSON

**E2E Tests from Press Release User Stories:**

- [ ] **Story: docker compose up starts all services** - Run `docker compose up -d`, verify `docker compose ps` shows api, worker, postgres, redis, enclave all running/healthy
- [ ] **Story: DB migrations create schema** - After startup, connect to postgres and verify documents and extracted_fields tables exist with correct columns
- [ ] **Story: mTLS certificate generation** - Run `scripts/generate_certs.sh`, verify ca.crt, ca.key, enclave.crt, enclave.key, parent.crt, parent.key all exist and are valid x509
- [ ] **Story: Health check endpoints** - `curl localhost:8000/health` returns `{"status": "healthy", "service": "api"}`; enclave health reachable from api container via mTLS
- [ ] **Story: Enclave network isolation** - From inside enclave container, confirm no route to external networks and no route to postgres/redis

**Manual Verification:**

- [ ] Stop and restart services with `docker compose down && docker compose up -d` -- everything comes back healthy
- [ ] Delete certs and re-generate -- services can restart with new certs
- [ ] Run `docker compose logs enclave` -- no error output

---

## Rollback Plan

This is a greenfield project with no existing code to break. Rollback is simply deleting generated files. If Docker Compose is misconfigured, `docker compose down -v` removes all containers and volumes for a clean restart.

## Open Questions

1. **Alembic autogenerate vs manual migrations**: For M0 the migration is written manually. Future milestones should decide whether to use `alembic revision --autogenerate` based on ORM model changes.
2. **Worker concurrency**: Default Celery concurrency (prefork, CPU count) is fine for M0. May need tuning in M2 when actual tasks exist.
3. **Enclave port**: Using 8443 for the enclave mTLS server. This is internal-only and not exposed to the host.

---

## Next Steps

If you approve this plan, proceed to implementation:

/pipe-implement docs/dev/legaldocs/m0-project-setup/implementation-plan.md

If you want changes:

- Tell me what to revise and I'll update the plan
- Ask questions about specific phases or decisions
