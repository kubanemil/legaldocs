---
date: 2026-03-29
researcher: claude-opus-4-6
branch: n/a (greenfield, no git repo yet)
repository: legaldocs
tags: docker-compose, postgresql, mtls, celery, redis, fastapi, alembic, project-structure
status: complete
---

# Research: M0 Project Setup

## Research Question

What are the best practices and technical patterns for setting up the LegalDocs project infrastructure, including Docker Compose with internal networks for enclave simulation, PostgreSQL schema for encrypted document storage with tenant isolation, mTLS certificate generation, Celery + Redis with FastAPI, Python monorepo structure, and Alembic migrations?

## Summary

This research covers six areas needed for the M0 milestone of the LegalDocs privacy-preserving document processing pipeline. The project is greenfield -- no existing codebase -- so all findings are based on external best practices and the architecture defined in PROJECT_PLAN.md.

Docker Compose supports an `internal: true` network flag that creates networks with no external connectivity, which directly maps to the enclave isolation requirement. The recommended pattern uses two networks: an external-facing network for the API/DB/Redis and an internal-only network bridged solely by the parent container to reach the enclave. PostgreSQL schema design for encrypted document storage should use `LargeBinary` columns for ciphertext blobs, a `tenant_id` column on every table, and simple enum-based status tracking. mTLS certificates require a three-tier generation script (CA, server cert for enclave, client cert for parent). Celery with Redis and FastAPI follows a well-established Docker Compose pattern with separate worker and API containers sharing the same codebase. The project structure should follow the layout already defined in PROJECT_PLAN.md with `parent/`, `enclave/`, and `client/` as top-level service directories. Alembic migrations should run via an entrypoint script, controllable by environment variable.

## Detailed Findings

### 1. Docker Compose Network Isolation for Enclave Simulation

The core requirement is that the enclave container has zero external network access, mirroring AWS Nitro Enclave's hardware isolation. Docker Compose provides this natively.

**Two-network architecture:**

```yaml
networks:
  external-net:
    driver: bridge
  enclave-net:
    driver: bridge
    internal: true
```

**Key behaviors of `internal: true`:**
- No default gateway is configured for containers on this network
- Containers cannot route to external networks or the internet
- Port exposure to the host is not possible
- Containers on the same internal network can communicate with each other by service name

**Service-to-network mapping:**

| Service    | external-net | enclave-net |
|------------|:------------:|:-----------:|
| API/Worker | yes          | yes         |
| PostgreSQL | yes          | no          |
| Redis      | yes          | no          |
| Enclave    | no           | yes         |

The parent (API/Worker) container bridges both networks, acting as the sole communication channel to the enclave. This mirrors the vsock pattern in AWS Nitro Enclaves where the parent EC2 instance is the only entity that can communicate with the enclave VM.

**Health checks:** Each service should define a Docker Compose `healthcheck` block. Dependencies should use `depends_on` with `condition: service_healthy` to ensure proper startup ordering.

```yaml
services:
  postgres:
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  api:
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
```

**Enclave container hardening (for M0 structure, full implementation in M1):**

```yaml
enclave:
  read_only: true
  tmpfs:
    - /tmp
  networks:
    - enclave-net
  # No ports exposed
```

### 2. PostgreSQL Schema Design for Encrypted Document Storage

The database stores only ciphertext and metadata -- never plaintext. The schema needs two tables as specified in PROJECT_PLAN.md: `documents` and `extracted_fields`.

**Recommended `documents` table:**

```sql
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id VARCHAR(64) NOT NULL,
    filename VARCHAR(255),
    content_type VARCHAR(128),
    encrypted_content BYTEA NOT NULL,        -- Client-encrypted document ciphertext
    encrypted_result BYTEA,                  -- Enclave-encrypted extraction results
    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_status CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED'))
);

CREATE INDEX idx_documents_tenant_id ON documents(tenant_id);
CREATE INDEX idx_documents_status ON documents(status);
CREATE INDEX idx_documents_tenant_status ON documents(tenant_id, status);
```

**Recommended `extracted_fields` table:**

```sql
CREATE TABLE extracted_fields (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tenant_id VARCHAR(64) NOT NULL,          -- Denormalized for query efficiency
    field_name VARCHAR(128) NOT NULL,
    encrypted_value BYTEA NOT NULL,          -- Encrypted extraction result
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_document FOREIGN KEY (document_id) REFERENCES documents(id)
);

CREATE INDEX idx_extracted_fields_document_id ON extracted_fields(document_id);
CREATE INDEX idx_extracted_fields_tenant_id ON extracted_fields(tenant_id);
```

**Design decisions:**

- **UUID primary keys**: Avoids sequential ID leakage across tenants.
- **`BYTEA` for encrypted content**: Since the encryption may produce binary output (not just base64), `BYTEA` is the safest column type. If using Fernet (base64 output), `TEXT` would also work, but `BYTEA` is more general.
- **`tenant_id` on both tables**: Denormalized onto `extracted_fields` for direct tenant-scoped queries without joins. This follows the multi-tenant best practice of having the tenant identifier on every table.
- **Status enum as CHECK constraint**: Simple and sufficient for MVP. Production might use a proper ENUM type.
- **No Row-Level Security for M0**: PROJECT_PLAN.md specifies `tenant_id` column + API-level scoping for MVP (Technical Decision #3). RLS would be a future enhancement.

### 3. mTLS Certificate Generation

mTLS requires three certificate sets: a Certificate Authority (CA), a server certificate (for the enclave), and a client certificate (for the parent).

**Certificate hierarchy:**

```
Root CA (self-signed)
├── Enclave Server Certificate (signed by CA)
│   - CN: enclave
│   - SAN: DNS:enclave (Docker service name)
└── Parent Client Certificate (signed by CA)
    - CN: parent
```

**Generation script outline (`scripts/generate_certs.sh`):**

```bash
#!/bin/bash
set -euo pipefail

CERT_DIR="./certs"
DAYS=3650  # Long-lived for local dev
KEY_SIZE=4096

mkdir -p "$CERT_DIR"

# 1. Generate CA
openssl genrsa -out "$CERT_DIR/ca.key" $KEY_SIZE
openssl req -new -x509 -days $DAYS -key "$CERT_DIR/ca.key" \
  -out "$CERT_DIR/ca.crt" -subj "/CN=LegalDocs Local CA"

# 2. Generate Enclave Server Certificate
openssl genrsa -out "$CERT_DIR/enclave.key" $KEY_SIZE
openssl req -new -key "$CERT_DIR/enclave.key" \
  -out "$CERT_DIR/enclave.csr" -subj "/CN=enclave"

# Create SAN config for Docker DNS resolution
cat > "$CERT_DIR/enclave-ext.cnf" <<EOF
[v3_req]
subjectAltName = DNS:enclave,DNS:localhost
EOF

openssl x509 -req -days $DAYS -in "$CERT_DIR/enclave.csr" \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/enclave.crt" -extfile "$CERT_DIR/enclave-ext.cnf" \
  -extensions v3_req

# 3. Generate Parent Client Certificate
openssl genrsa -out "$CERT_DIR/parent.key" $KEY_SIZE
openssl req -new -key "$CERT_DIR/parent.key" \
  -out "$CERT_DIR/parent.csr" -subj "/CN=parent"
openssl x509 -req -days $DAYS -in "$CERT_DIR/parent.csr" \
  -CA "$CERT_DIR/ca.crt" -CAkey "$CERT_DIR/ca.key" -CAcreateserial \
  -out "$CERT_DIR/parent.crt"

# Cleanup CSRs
rm -f "$CERT_DIR"/*.csr "$CERT_DIR"/*.cnf "$CERT_DIR"/*.srl

echo "mTLS certificates generated in $CERT_DIR/"
```

**Key considerations:**
- The enclave server cert needs a Subject Alternative Name (SAN) matching the Docker Compose service name (`enclave`) so TLS hostname verification passes.
- The `certs/` directory should be `.gitignore`d.
- Certificates are mounted as read-only volumes into the containers.
- 4096-bit RSA keys are standard for self-signed local dev certs. For faster generation, 2048-bit is also acceptable for an MVP.

**Volume mounting in Docker Compose:**

```yaml
services:
  api:
    volumes:
      - ./certs/ca.crt:/certs/ca.crt:ro
      - ./certs/parent.crt:/certs/client.crt:ro
      - ./certs/parent.key:/certs/client.key:ro

  enclave:
    volumes:
      - ./certs/ca.crt:/certs/ca.crt:ro
      - ./certs/enclave.crt:/certs/server.crt:ro
      - ./certs/enclave.key:/certs/server.key:ro
```

### 4. Celery + Redis Configuration with FastAPI

The standard pattern runs the FastAPI API server and Celery worker as separate processes from the same codebase, with Redis as both broker and result backend.

**Celery application setup (`parent/workers/celery_app.py`):**

```python
from celery import Celery

celery_app = Celery(
    "legaldocs",
    broker="redis://redis:6379/0",
    backend="redis://redis:6379/1",
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,        # 5 min hard limit
    task_soft_time_limit=240,   # 4 min soft limit
    result_expires=3600,        # Results expire after 1 hour
)

celery_app.autodiscover_tasks(["parent.workers"])
```

**Docker Compose services:**

```yaml
services:
  redis:
    image: redis:7-alpine
    networks:
      - external-net
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  api:
    build:
      context: .
      dockerfile: parent/Dockerfile
    command: uvicorn parent.api.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    networks:
      - external-net
      - enclave-net
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy

  worker:
    build:
      context: .
      dockerfile: parent/Dockerfile
    command: celery -A parent.workers.celery_app worker --loglevel=info
    networks:
      - external-net
      - enclave-net
    depends_on:
      redis:
        condition: service_healthy
```

**Key patterns:**
- The API and worker share the same Docker image (same `parent/Dockerfile`) but run different commands.
- Redis uses separate databases (db 0 for broker, db 1 for results) to avoid key collisions.
- The worker needs access to both networks: `external-net` for Redis/PostgreSQL and `enclave-net` for forwarding ciphertext to the enclave.
- Environment variables should configure broker/backend URLs rather than hardcoding them.

**FastAPI integration:**

```python
from fastapi import FastAPI
from parent.workers.celery_app import celery_app

app = FastAPI(title="LegalDocs API")

@app.post("/documents")
async def create_document(...):
    # Save to DB, then queue task
    celery_app.send_task("parent.workers.tasks.process_document", args=[doc_id])
```

### 5. Project Structure

PROJECT_PLAN.md already defines the target structure. Based on Python monorepo best practices, the following refinements are recommended:

```
legaldocs/
├── docker-compose.yml
├── .env.example                 # Template for environment variables
├── .gitignore
├── parent/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── __init__.py              # Makes parent a Python package
│   ├── api/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI app, health check endpoint
│   │   ├── schemas.py           # Pydantic request/response models
│   │   └── dependencies.py      # DB session, config injection
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── celery_app.py        # Celery instance configuration
│   │   └── tasks.py             # Task definitions
│   ├── models/
│   │   ├── __init__.py
│   │   └── models.py            # SQLAlchemy ORM models
│   └── db.py                    # Database engine/session factory
├── enclave/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── server.py                # mTLS server (health check for M0)
│   ├── extractor.py             # Placeholder for M2
│   └── crypto.py                # Placeholder for M1
├── client/
│   ├── requirements.txt
│   └── client.py                # Placeholder for M1
├── scripts/
│   ├── generate_certs.sh        # mTLS certificate generation
│   └── run_migrations.sh        # Alembic migration runner
├── alembic/
│   ├── alembic.ini              # Alembic configuration
│   ├── env.py                   # Migration environment
│   └── versions/                # Migration files
│       └── 001_initial_schema.py
└── certs/                       # Generated certificates (gitignored)
```

**Key conventions:**
- Each service directory (`parent/`, `enclave/`) is a Python package with `__init__.py`.
- Shared configuration (DB URL, Redis URL) flows through environment variables defined in `docker-compose.yml` or `.env`.
- The `parent/Dockerfile` copies the entire `parent/` directory and installs from `parent/requirements.txt`.
- Alembic lives at the project root since it is a development tool, not part of a specific service.

### 6. Alembic Migration Setup

**Recommended pattern for this project:** Run migrations via an entrypoint script, controllable by environment variable. For local dev, migrations auto-run on container startup. For production scenarios, they would be run separately.

**Alembic initialization:**

```
alembic/
├── alembic.ini          # Points to env.py, configures DB URL
├── env.py               # Reads DB URL from environment
└── versions/
    └── 001_initial_schema.py
```

**`alembic.ini` key settings:**

```ini
[alembic]
script_location = alembic
sqlalchemy.url = postgresql://postgres:postgres@localhost:5432/legaldocs
```

**`env.py` should read the database URL from environment:**

```python
import os
from alembic import context
from sqlalchemy import create_engine

def get_url():
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/legaldocs"
    )

def run_migrations_online():
    engine = create_engine(get_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
```

**Entrypoint script (`scripts/run_migrations.sh`):**

```bash
#!/bin/bash
set -euo pipefail

echo "Running database migrations..."
alembic upgrade head
echo "Migrations complete."
```

**Docker Compose integration:**

Option A -- Separate init container (recommended):

```yaml
services:
  migrate:
    build:
      context: .
      dockerfile: parent/Dockerfile
    command: alembic upgrade head
    depends_on:
      postgres:
        condition: service_healthy
    networks:
      - external-net
    environment:
      DATABASE_URL: postgresql://postgres:postgres@postgres:5432/legaldocs
```

Option B -- Entrypoint script on API container:

```yaml
services:
  api:
    command: >
      bash -c "alembic upgrade head && uvicorn parent.api.main:app --host 0.0.0.0 --port 8000"
```

Option A is cleaner because it separates concerns and avoids coupling migrations with application startup. The migration container runs once and exits.

## Architecture Notes

The M0 setup establishes the foundational isolation model that all subsequent milestones build on:

1. **Network boundary**: The `enclave-net` internal network is the simulation of the Nitro Enclave's hardware isolation. This is the single most important configuration -- if the enclave can reach external networks, the privacy model breaks.

2. **Data boundary**: PostgreSQL stores only ciphertext (`BYTEA` columns). The schema is designed so that even with full database access, no plaintext can be recovered. This is enforced by the encryption in M1 but the schema must support it from M0.

3. **Communication boundary**: mTLS certificates establish mutual authentication between parent and enclave. The parent proves its identity to the enclave (so the enclave only accepts requests from the legitimate parent), and the enclave proves its identity to the parent (so the parent knows it is talking to the real enclave).

4. **Task boundary**: Celery tasks on the parent side will forward ciphertext to the enclave via mTLS. The enclave has no direct access to Redis or PostgreSQL -- all data flows through the parent as an intermediary, mirroring the vsock channel in production.

## Open Questions

1. **Should `alembic.ini` live at the project root or inside `alembic/`?** Convention is project root for `alembic.ini` with `script_location = alembic` pointing to the migrations directory. This keeps `alembic` commands runnable from the project root.

2. **Health check endpoint format**: The press release mentions health checks but does not specify format. Recommendation: simple JSON `{"status": "healthy", "service": "<name>"}` for consistency. This needs a decision during planning.

3. **Enclave server framework for M0**: The enclave needs a minimal mTLS server for the health check. Options include raw `ssl` module with `http.server`, or a lightweight framework like Flask. Since the enclave should have minimal dependencies, raw Python stdlib may be preferred. This needs a decision during planning.

4. **Docker Compose version**: Modern Docker Compose (v2+) no longer requires the `version:` key in the YAML file. The implementation should use the current Compose specification without the legacy version field.

5. **Python version**: PROJECT_PLAN.md does not specify. Python 3.11 or 3.12 are recommended as the base image for stability and performance. Python 3.13 is available but some dependencies may not yet have wheels.

---

## Next Steps

If you are satisfied with this research, proceed to planning:

/pipe-plan-implementation --project legaldocs --ref m0 --research docs/dev/legaldocs/m0-project-setup/research.md project setup

If you need more research:

- Ask follow-up questions and I will append to this document
- Or request a new research topic with /pipe-research --project legaldocs --ref m0 [new-topic]
