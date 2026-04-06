# Project Plan: Privacy-Preserving Legal Document Processing Pipeline with Blockchain Anchoring

## Vision & Goals

**Problem:** Process legal documents (extract structured data from unstructured text) while guaranteeing that documents remain private — not visible to the API server, developers, or anyone except the document owner and the verified processing enclave.

**Goal:** A locally runnable MVP that mirrors the architecture of a production AWS Nitro Enclave-based pipeline, using Docker to simulate enclave isolation, client-side encryption for end-to-end privacy, and Ethereum blockchain anchoring for tamper-proof audit trails.

**Non-Goals:**
- High-quality NLP/ML extraction (regex is fine)
- Scale/performance testing
- Real cloud deployment or real AWS KMS
- Real hardware-based attestation (simulated with certs)
- Production-grade cryptography libraries

---

## Architecture

### System Overview

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
└──────────────┘      │  ┌──────┴───────┐           └─────────────────────┘  │
                       │  │  PostgreSQL  │             enclave-net (internal) │
                       │  │  Redis       │                                    │
                       │  └──────────────┘                                    │
                       └──────────────────────────────────────────────────────┘
```

### Data Flow

```
Client                     Parent (API/Worker)          Enclave                Simulated KMS
  │                              │                        │                        │
  │  1. GET /attestation         │                        │                        │
  │─────────────────────────────>│──── mTLS ────────────>│                        │
  │  2. Attestation doc          │                        │                        │
  │<─────────────────────────────│<───────────────────────│                        │
  │                              │                        │                        │
  │  3. Verify attestation       │                        │                        │
  │                              │                        │                        │
  │  4. Encrypt doc with         │                        │                        │
  │     enclave's public key     │                        │                        │
  │                              │                        │                        │
  │  5. POST /documents          │                        │                        │
  │     {tenant_id, ciphertext}  │                        │                        │
  │─────────────────────────────>│  6. Store ciphertext   │                        │
  │                              │     in DB (PENDING)    │                        │
  │                              │  7. Queue Celery task  │                        │
  │                              │  8. Send ciphertext    │                        │
  │                              │     over mTLS ────────>│                        │
  │                              │                        │  9. Decrypt doc        │
  │                              │                        │ 10. Extract fields     │
  │                              │                        │ 11. Encrypt results    │
  │                              │  12. Encrypted results │                        │
  │                              │<───────────────────────│                        │
  │                              │  13. Store encrypted   │                        │
  │                              │      results in DB     │                        │
  │                              │      (COMPLETED)       │                        │
  │                              │                        │                        │
  │  14. GET /documents/{id}     │                        │                        │
  │─────────────────────────────>│  15. Return encrypted  │                        │
  │  16. Encrypted results       │      results           │                        │
  │<─────────────────────────────│                        │                        │
  │  17. Decrypt locally         │                        │                        │
```

### How It Maps to AWS Production

| AWS Production               | Local MVP                                          |
|------------------------------|----------------------------------------------------|
| EC2 Parent Instance          | Parent container (FastAPI + Celery worker)          |
| Nitro Enclave VM             | Docker container on internal-only network           |
| vsock channel                | mTLS over Docker internal network                   |
| Nitro Attestation (hardware) | Simulated attestation (self-signed cert + metadata) |
| AWS KMS                      | Simulated KMS (enclave-side key management)         |
| KMS key policy with PCR      | Cert verification (simplified)                      |
| RDS PostgreSQL               | PostgreSQL container                                |
| SQS / Celery on SQS          | Redis + Celery                                      |
| S3 (raw doc storage)         | PostgreSQL (stores encrypted blobs)                 |
| Ethereum L1/L2               | Ganache (local deterministic chain)                 |

---

## Technical Decisions

| #  | Decision                  | Choice                                     | Rationale                                                                 |
|----|---------------------------|--------------------------------------------|---------------------------------------------------------------------------|
| 1  | Enclave simulation        | Docker `--internal` network + mTLS         | Mirrors Nitro pattern: isolated VM with single channel to parent          |
| 2  | Pipeline orchestration    | Celery chains                              | Standard production pattern for ordered multi-stage processing            |
| 3  | Tenant isolation          | `tenant_id` column + API-level scoping     | Simplest model for MVP; production would use PG schemas or RLS           |
| 4  | Communication             | Parent→enclave via mTLS socket             | Enclave has no broker access; parent sends ciphertext, receives ciphertext|
| 5  | API layer                 | FastAPI                                    | Lightweight, async-friendly                                               |
| 6  | Extraction                | Regex (parties, dates)                     | Focus is on pipeline architecture, not NLP quality                        |
| 7  | Client-side encryption    | RSA/AES hybrid (enclave public key)        | Simulates KMS envelope encryption pattern                                 |
| 8  | Attestation simulation    | Enclave returns signed metadata + cert     | Simulates Nitro attestation document without hardware                     |
| 9  | Database stores           | Only ciphertext + metadata, never plaintext| Mirrors production: DB is outside trust boundary                          |
| 10 | Blockchain anchoring      | Solidity contract on local Ganache          | Immutable audit trail; keccak-256 hashes prove document integrity         |
| 11 | On-chain data             | Only hashes, never content                  | Blockchain sees zero plaintext; privacy preserved on public ledger        |

---

## Privacy Guarantees (What This Demonstrates)

| Component                  | Sees plaintext? | Why                                                     |
|----------------------------|-----------------|---------------------------------------------------------|
| Client                     | Yes             | It's their document                                     |
| Parent (API/Worker)        | **No**          | Only handles ciphertext; doesn't have decryption key    |
| PostgreSQL                 | **No**          | Stores encrypted blobs and metadata only                |
| Redis                      | **No**          | Task queue contains doc IDs, not content                |
| Enclave                    | Yes             | Decrypts inside isolated container                      |
| Blockchain                 | **No**          | Only stores keccak-256 hashes of ciphertext             |
| Developer with host access | **No**          | Can't read enclave memory; parent only has ciphertext   |

---

## Milestones

| ID | Milestone                  | Description                                                          |
|----|----------------------------|----------------------------------------------------------------------|
| M0 | **Project Setup**          | Repo structure, Docker Compose, services running, DB schema          |
| M1 | **Enclave + Encryption**   | Enclave container with key pair, client-side encryption, attestation |
| M2 | **Processing Pipeline**    | Celery chain: receive ciphertext → forward to enclave → store result |
| M3 | **Tenant Isolation**       | Multi-tenant scoping on API and DB level                             |
| M4 | **Verification & Demo**    | Prove privacy works, demo script, logging                            |
| M5 | **Blockchain Anchoring**   | On-chain document hash notarization via Ethereum smart contract      |

> **Note:** Encryption is in M1, not bolted on later. The parent should never see plaintext at any milestone.

---

## Epics & User Stories

### M0: Project Setup

| ID   | Story                                                                                   | Size |
|------|-----------------------------------------------------------------------------------------|------|
| M0.1 | As a developer, I can run `docker compose up` and have all services start (API, Redis, PostgreSQL, enclave) | M    |
| M0.2 | As a developer, I can run DB migrations to create the schema (documents, extracted_fields tables) | S    |
| M0.3 | As a developer, I can generate mTLS certificates via a script                           | S    |

### M1: Enclave + Encryption

| ID   | Story                                                                                   | Size |
|------|-----------------------------------------------------------------------------------------|------|
| M1.1 | As the enclave, I generate a key pair on startup and expose an attestation endpoint over mTLS | M    |
| M1.2 | As a client, I can request and verify the enclave's attestation (get its public key)    | S    |
| M1.3 | As a client, I can encrypt a document using the enclave's public key before uploading   | S    |
| M1.4 | As the enclave, I can decrypt a document that was encrypted with my public key           | S    |
| M1.5 | As the enclave, I run in a Docker container with no internet, no DB access, read-only filesystem, tmpfs only | S    |

### M2: Processing Pipeline

| ID   | Story                                                                                   | Size |
|------|-----------------------------------------------------------------------------------------|------|
| M2.1 | As a client, I can POST an encrypted document to the API and get a document ID back     | S    |
| M2.2 | As the parent worker, I forward the ciphertext to the enclave over mTLS (never decrypting it) | M    |
| M2.3 | As the enclave, I decrypt the document, extract parties and dates via regex, and return encrypted results | M    |
| M2.4 | As the parent worker, I store the encrypted results in PostgreSQL and update the document status | S    |
| M2.5 | As a client, I can GET a document's encrypted results and decrypt them locally           | S    |

### M3: Tenant Isolation

| ID   | Story                                                                                   | Size |
|------|-----------------------------------------------------------------------------------------|------|
| M3.1 | As a client, I submit documents with a tenant identifier                                | S    |
| M3.2 | As a client, I can only retrieve documents belonging to my tenant                       | S    |
| M3.3 | As the system, all DB queries are scoped by tenant_id                                   | S    |

### M4: Verification & Demo

| ID   | Story                                                                                   | Size |
|------|-----------------------------------------------------------------------------------------|------|
| M4.1 | As a developer, I can run a verification script that proves the parent never sees plaintext | M    |
| M4.2 | As a developer, I can see structured logs showing a document's journey through the pipeline | S    |
| M4.3 | As a developer, I can run a demo script that submits sample docs as different tenants and shows end-to-end results | M    |
| M4.4 | As a developer, I can verify the enclave container cannot reach external services        | S    |

### M5: Blockchain Anchoring

| ID   | Story                                                                                   | Size |
|------|-----------------------------------------------------------------------------------------|------|
| M5.1 | As the system, after processing completes I compute keccak-256 hashes of encrypted content and results | S    |
| M5.2 | As the system, I deploy a DocumentRegistry Solidity contract to a local Ganache chain   | S    |
| M5.3 | As the system, I anchor document hashes on-chain and store the tx_hash in PostgreSQL    | M    |
| M5.4 | As a client, I can verify a document's on-chain anchor matches the hashes I received    | S    |
| M5.5 | As a developer, the API response includes content_hash, result_hash, and tx_hash fields | S    |

---

## Risks

| Risk                                  | Impact | Mitigation                                                         |
|---------------------------------------|--------|--------------------------------------------------------------------|
| mTLS setup complexity                 | Medium | Generate certs in a setup script; start simple, iterate            |
| Enclave can't access Celery broker    | Medium | Parent Celery task makes synchronous mTLS call to enclave server   |
| Crypto implementation correctness     | Low    | Use `cryptography` library, not hand-rolled crypto                 |
| Docker internal network debugging     | Low    | Build a health-check endpoint in enclave; verify isolation early   |
| Ganache chain reset on restart        | Low    | Contract auto-deploys; production would use persistent chain       |
| Gas costs on public chains            | Medium | Use L2 (Base, Arbitrum) for low-cost anchoring in production       |

---

## Project Structure

```
legaldocs/
├── docker-compose.yml
├── contracts/
│   ├── DocumentRegistry.sol  # Solidity contract for hash anchoring
│   └── DocumentRegistry.json # Pre-compiled ABI + bytecode
├── parent/
│   ├── Dockerfile
│   ├── blockchain.py         # Web3.py integration
│   ├── api/                  # FastAPI application
│   │   ├── main.py           # API endpoints
│   │   └── schemas.py        # Pydantic models
│   ├── workers/              # Celery tasks
│   │   ├── celery_app.py     # Celery configuration
│   │   └── tasks.py          # Pipeline tasks (ingest, forward, store, anchor)
│   ├── models/               # SQLAlchemy models
│   │   └── models.py         # Document, ExtractedField tables
│   └── requirements.txt
├── enclave/
│   ├── Dockerfile
│   ├── server.py             # mTLS socket server
│   ├── extractor.py          # Regex extraction (parties, dates)
│   ├── crypto.py             # Key generation, encrypt/decrypt
│   └── requirements.txt
├── client/
│   ├── client.py             # Client SDK (encrypt, upload, fetch, decrypt)
│   └── requirements.txt
├── scripts/
│   ├── generate_certs.sh     # Generate mTLS certificates
│   ├── demo.py               # End-to-end demo
│   └── verify_privacy.py     # Prove parent can't see plaintext
├── alembic/                  # DB migrations
│   └── ...
└── certs/                    # mTLS certificates (gitignored)
```

---

## Next Steps

Milestones M0–M5 are implemented. Recommended areas for further development:

- **Production chain deployment**: Replace Ganache with a testnet (Sepolia) or L2 (Base, Arbitrum)
- **IPFS integration**: Store encrypted documents on IPFS, anchor CIDs on-chain
- **ZK proofs**: Generate zero-knowledge proofs of correct extraction without revealing content
- **Token-gated access**: Use ERC-721 or ERC-1155 for document access control
