# LegalDocs: Privacy-Preserving Legal Document Processing with Blockchain Anchoring

A locally runnable MVP that demonstrates privacy-preserving document processing using [AWS Nitro Enclave](https://aws.amazon.com/ec2/nitro/nitro-enclaves/) patterns with **Ethereum blockchain anchoring** for tamper-proof audit trails — all without requiring AWS or a public chain.

The core guarantees:
- **The API server never sees your documents in plaintext.** Documents are encrypted client-side before upload. Only a network-isolated enclave container can decrypt and process them.
- **Every processed document is anchored on-chain.** Keccak-256 hashes of encrypted content and results are recorded in a Solidity smart contract, creating an immutable, verifiable audit trail.

## Architecture

```
┌──────────────┐      ┌──────────────────────────────────────────────────────┐
│    Client     │      │               docker compose                         │
│               │      │                                                      │
│ 1. Request    │      │  ┌──────────────┐           ┌─────────────────────┐  │
│    attestation│─────>│  │    Parent     │   mTLS    │  Enclave Container  │  │
│ 2. Verify it  │<─────│  │   (FastAPI +  │<────────>│                     │  │
│ 3. Encrypt    │      │  │    Celery     │  vsock    │  - Holds private    │  │
│    doc locally│      │  │    worker)    │  (simul.) │    key              │  │
│ 4. Send       │      │  │              │           │  - Decrypts docs    │  │
│    ciphertext │─────>│  │  ONLY sees   │           │  - Extracts fields  │  │
│               │      │  │  encrypted   │           │  - Encrypts results │  │
│ 5. Receive    │<─────│  │  blobs       │           │                     │  │
│    encrypted  │      │  └──────┬───────┘           │  No DB access       │  │
│    results    │      │         │                    │  No internet        │  │
│ 6. Decrypt    │      │   external-net              │  tmpfs only         │  │
│    locally    │      │         │                    │  read-only root     │  │
│ 7. Verify     │      │  ┌──────┴───────┐           └─────────────────────┘  │
│    on-chain   │      │  │  PostgreSQL  │                                    │
│    hash       │      │  │  Redis       │           ┌─────────────────────┐  │
└──────────────┘      │  │  Ganache     │           │  Ethereum (local)   │  │
                       │  └──────────────┘           │  DocumentRegistry   │  │
                       │                              │  smart contract     │  │
                       │                              └─────────────────────┘  │
                       └──────────────────────────────────────────────────────┘
```

### How It Maps to AWS Production

| AWS Production | Local MVP |
|---|---|
| EC2 Parent Instance | Parent container (FastAPI + Celery) |
| Nitro Enclave VM | Docker container on internal-only network |
| vsock channel | mTLS over Docker internal network |
| Nitro Attestation (hardware) | Simulated attestation (signed cert + metadata) |
| AWS KMS | Enclave-side key management |
| RDS PostgreSQL | PostgreSQL container |
| SQS / Celery on SQS | Redis + Celery |
| Ethereum L1/L2 | Ganache local chain |

### Privacy Guarantees

| Component | Sees plaintext? | Why |
|---|---|---|
| Client | Yes | It's their document |
| Parent (API/Worker) | **No** | Only handles ciphertext |
| PostgreSQL | **No** | Stores encrypted blobs only |
| Redis | **No** | Task queue has doc IDs, not content |
| Blockchain | **No** | Only stores keccak-256 hashes |
| Enclave | Yes | Decrypts in isolated memory |
| Developer with host access | **No** | Can't read enclave memory or get keys |

## Quick Start

### Prerequisites

- Docker and Docker Compose v2+
- Python 3.12+
- `openssl` CLI

### Run

```bash
# 1. Generate mTLS certificates
bash scripts/generate_certs.sh

# 2. Start all services (including local Ethereum via Ganache)
docker compose up -d

# 3. Run the demo (submits docs as two tenants, shows extraction results + tx hashes)
pip install cryptography requests
python scripts/demo.py

# 4. Verify privacy (proves parent never sees plaintext)
pip install psycopg2-binary
python scripts/verify_privacy.py

# 5. Stop
docker compose down
```

## How It Works

### Data Flow

1. **Client requests attestation** from the enclave (via API proxy)
2. **Client verifies** the attestation signature against the CA certificate
3. **Client encrypts** the document using the enclave's public key (RSA-OAEP + AES-256-GCM envelope encryption)
4. **Client submits** the ciphertext to `POST /documents`
5. **API stores** the encrypted blob in PostgreSQL, queues a Celery task
6. **Celery worker** forwards the ciphertext to the enclave over mTLS
7. **Enclave decrypts**, extracts parties and dates via regex, encrypts results
8. **Worker stores** encrypted results in PostgreSQL
9. **Worker anchors** keccak-256 hashes of content + results on the DocumentRegistry smart contract
10. **Client retrieves** encrypted results via `GET /documents/{id}` and decrypts locally
11. **Client can verify** the on-chain anchor matches the document hashes

### Blockchain Anchoring

After processing completes, the pipeline computes keccak-256 hashes of the encrypted content and encrypted results, then calls `DocumentRegistry.anchorDocument()` on the local Ethereum chain. This creates an immutable record that:

- Proves the document existed at a specific time
- Proves the processing results haven't been tampered with
- Links content hash, result hash, tenant, and timestamp in a single on-chain record

The `DocumentRegistry` contract auto-deploys on first use. The transaction hash is stored in the `documents` table alongside the content/result hashes.

### Encryption

- **Envelope encryption**: RSA-2048 OAEP wraps a random AES-256-GCM data encryption key
- **Wire format**: `[4B DEK length][encrypted DEK][12B nonce][AES-GCM ciphertext + tag]`
- **Attestation**: Enclave signs its public key + metadata with its mTLS private key; client verifies against CA

### Tenant Isolation

All document endpoints require an `X-Tenant-ID` header. Tenant A cannot see, list, or retrieve tenant B's documents (returns 404, not 403, to avoid leaking existence).

## Project Structure

```
legaldocs/
├── docker-compose.yml          # All services: postgres, redis, ganache, api, worker, enclave
├── contracts/
│   ├── DocumentRegistry.sol    # Solidity smart contract for hash anchoring
│   ├── DocumentRegistry.json   # Pre-compiled ABI + bytecode
│   └── compile.py              # Optional: recompile with solc
├── parent/
│   ├── Dockerfile              # Shared image for API + worker
│   ├── blockchain.py           # Web3.py integration with DocumentRegistry
│   ├── api/
│   │   ├── main.py             # FastAPI endpoints
│   │   ├── schemas.py          # Pydantic models
│   │   └── dependencies.py     # Tenant extraction, DB session
│   ├── workers/
│   │   ├── celery_app.py       # Celery configuration
│   │   └── tasks.py            # Pipeline: ingest → forward → store → anchor
│   ├── models/models.py        # SQLAlchemy ORM (Document, ExtractedField)
│   ├── db.py                   # Database connection
│   └── logging_config.py       # Structured JSON logging
├── enclave/
│   ├── Dockerfile              # Minimal isolated image
│   ├── server.py               # mTLS server (/health, /attestation, /process, /decrypt)
│   ├── crypto.py               # RSA/AES envelope encryption
│   └── extractor.py            # Regex extraction (parties, dates)
├── client/
│   └── client.py               # SDK: encrypt, submit, poll, decrypt
├── alembic/                    # Database migrations
├── scripts/
│   ├── generate_certs.sh       # mTLS certificate generation
│   ├── demo.py                 # Multi-tenant demo
│   └── verify_privacy.py       # Privacy proof script
└── docs/                       # Additional documentation
```

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Service health check |
| `GET` | `/attestation` | None | Proxy enclave attestation (public key + signature) |
| `POST` | `/documents` | `X-Tenant-ID` | Submit encrypted document for processing |
| `GET` | `/documents` | `X-Tenant-ID` | List tenant's documents (optional `?status=` filter) |
| `GET` | `/documents/{id}` | `X-Tenant-ID` | Get document status, encrypted results, and tx_hash |
| `POST` | `/decrypt` | None | Proxy decryption request to enclave |

### Response Fields (Document)

```json
{
  "id": "uuid",
  "status": "COMPLETED",
  "encrypted_result": "base64...",
  "content_hash": "0x...",
  "result_hash": "0x...",
  "tx_hash": "0x...",
  "created_at": "2026-04-13T...",
  "updated_at": "2026-04-13T..."
}
```

## Tech Stack

- **Python 3.12**, **FastAPI**, **Celery**, **SQLAlchemy**, **Alembic**
- **PostgreSQL 16**, **Redis 7**
- **Solidity 0.8.20**, **Web3.py**, **Ganache** (local Ethereum)
- **Docker Compose** with internal network isolation
- **cryptography** library for RSA/AES operations
- **mTLS** for parent-enclave communication
