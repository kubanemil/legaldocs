---
date: 2026-03-29
author: claude-opus-4-6
status: draft
branch: main
related_research: docs/dev/legaldocs/m1-enclave-encryption/research.md
---

# Plan: M1 Enclave + Encryption

## Overview

This plan implements the foundational encryption layer for the legal document processing pipeline. The enclave generates an RSA key pair on startup and exposes a simulated attestation endpoint over mTLS. The client SDK fetches attestation, verifies it, and encrypts documents using hybrid RSA/AES envelope encryption. The parent API proxies attestation requests but never holds decryption keys, enforcing the privacy guarantee architecturally.

The implementation is divided into four phases: (1) enclave crypto module, (2) enclave attestation and decrypt endpoints, (3) client SDK, and (4) integration verification proving the parent never sees plaintext.

## Architecture Decisions

- **RSA-2048 key size**: Faster generation (~0.1s vs ~1-2s for 4096), sufficient for local MVP simulation. Configurable via `ENCLAVE_RSA_KEY_SIZE` environment variable.
- **Envelope encryption format**: Binary wire format with length-prefixed encrypted DEK + 12-byte nonce + AES-256-GCM ciphertext (includes auth tag). Simple to parse, no external serialization dependency.
- **Attestation format**: JSON with `payload` object + `signature` + `certificate`. Signed with the enclave's mTLS private key. Client verifies signature against the CA certificate chain.
- **Custom HTTPServer subclass**: `EnclaveHTTPServer(HTTPServer)` holds the crypto manager as a server attribute, accessible via `self.server.crypto` in the handler. Cleaner than module globals.
- **`/decrypt` endpoint in M1**: Added now so end-to-end round-trip is testable. M2 Celery worker will call this endpoint.
- **`httpx` for parent proxying**: Async-compatible HTTP client for FastAPI to forward attestation requests to the enclave over mTLS.
- **No new Docker/network changes**: Enclave isolation (internal-only network, read-only fs, tmpfs) is already configured in M0's `docker-compose.yml`.

## Prerequisites

- [ ] M0 complete: Docker Compose runs all services, mTLS certs generated via `scripts/generate_certs.sh`
- [ ] `certs/` directory populated with `ca.crt`, `ca.key`, `enclave.crt`, `enclave.key`, `parent.crt`, `parent.key`

---

## Phase 1: Enclave Crypto Module

### Goal

Implement key pair generation and hybrid envelope encryption/decryption in `enclave/crypto.py`. This is the core cryptographic foundation -- no server changes yet.

### Files to Modify

- `enclave/crypto.py` - Replace placeholder with full crypto module

### Module Design

**`CryptoManager`** - Manages RSA key pair lifecycle and provides encrypt/decrypt operations.

Key functions:

- `__init__(key_size: int = 2048)` - Generates RSA key pair in memory on instantiation
- `get_public_key_pem() -> bytes` - Returns PEM-encoded public key for attestation
- `decrypt_document(encrypted_blob: bytes) -> bytes` - Decrypts an envelope-encrypted document using the private key
- `encrypt_document(plaintext: bytes, public_key_pem: bytes) -> bytes` - Static/classmethod for envelope encryption (used by tests and client)

### Data Structures

```
Wire format (binary):
[4 bytes: encrypted_dek_length (big-endian)] +
[N bytes: RSA-OAEP encrypted DEK] +
[12 bytes: AES-GCM nonce] +
[remaining: AES-256-GCM ciphertext with appended 16-byte auth tag]
```

### Integration

- No integration with other modules yet -- this phase is self-contained
- The `CryptoManager` instance will be created in Phase 2 during server startup

### Success Criteria

**Automated Verification:**

```bash
cd enclave && python -c "
from crypto import CryptoManager
cm = CryptoManager(key_size=2048)
pub = cm.get_public_key_pem()
plaintext = b'Hello legal document'
encrypted = CryptoManager.encrypt_document(plaintext, pub)
decrypted = cm.decrypt_document(encrypted)
assert decrypted == plaintext
print('Phase 1: PASS')
"
```

**Manual Verification:**

- [ ] `CryptoManager` generates a key pair without errors
- [ ] Encrypting then decrypting recovers original plaintext
- [ ] Decrypting with the wrong key raises an exception
- [ ] Empty document encrypts/decrypts correctly
- [ ] Large document (1MB) encrypts/decrypts correctly

---

## Phase 2: Enclave Attestation and Decrypt Endpoints

### Goal

Add `/attestation` (GET) and `/decrypt` (POST) endpoints to the enclave mTLS server. The attestation endpoint returns the enclave's public key plus signed metadata. The decrypt endpoint accepts an encrypted blob and returns decrypted plaintext.

### Files to Modify

- `enclave/server.py` - Add `EnclaveHTTPServer` subclass, `/attestation` and `/decrypt` routes, initialize `CryptoManager` on startup

### Module Design

**`EnclaveHTTPServer(HTTPServer)`** - Custom server class holding the `CryptoManager` instance.

- `__init__(server_address, handler_class, crypto_manager)` - Stores crypto manager as `self.crypto`

**`EnclaveHandler` updates:**

- `do_GET /attestation` - Returns JSON attestation document: `{ payload: { enclave_id, public_key, timestamp, platform }, signature, certificate }`
- `do_POST /decrypt` - Reads encrypted blob from request body, returns decrypted bytes or error JSON

**Attestation signing:** Uses the enclave's mTLS private key (`/certs/enclave.key`) to sign the payload JSON bytes with PKCS1v15 + SHA256. The response includes the enclave's mTLS certificate (`/certs/enclave.crt`) so the client can verify.

### Data Structures

```
Attestation response (JSON):
{
  "payload": {
    "enclave_id": "<uuid4>",
    "public_key": "<PEM string>",
    "timestamp": "<ISO 8601>",
    "platform": "docker-simulation"
  },
  "signature": "<base64-encoded PKCS1v15-SHA256 signature of payload JSON>",
  "certificate": "<PEM string of enclave mTLS cert>"
}

Decrypt request: raw bytes (Content-Type: application/octet-stream)
Decrypt response (success): raw bytes (Content-Type: application/octet-stream)
Decrypt response (error): JSON { "error": "<message>" } with 400 status
```

### Integration

- `run_server()` creates a `CryptoManager` instance and passes it to `EnclaveHTTPServer`
- Handler accesses crypto via `self.server.crypto`
- Attestation signing loads mTLS key from `ENCLAVE_KEY_PATH` env var (same key used for SSL)
- Attestation certificate loaded from `ENCLAVE_CERT_PATH` env var

### Success Criteria

**Automated Verification:**

```bash
# Start enclave, then from a machine with parent certs:
curl --cacert certs/ca.crt --cert certs/parent.crt --key certs/parent.key \
  https://enclave:8443/attestation | python -m json.tool

curl --cacert certs/ca.crt --cert certs/parent.crt --key certs/parent.key \
  https://enclave:8443/health
```

**Manual Verification:**

- [ ] `/attestation` returns valid JSON with all required fields
- [ ] `signature` field verifies against the `certificate` and `payload`
- [ ] `public_key` in attestation is a valid PEM RSA public key
- [ ] `/decrypt` with a valid encrypted blob returns the original plaintext
- [ ] `/decrypt` with garbage data returns a 400 error with message
- [ ] `/health` still works as before

---

## Phase 3: Client SDK

### Goal

Implement the client SDK that requests attestation from the parent API, verifies it, encrypts documents, and can decrypt results. Also add the parent API `/attestation` proxy endpoint.

### Files to Modify

- `client/client.py` - Replace placeholder with full client SDK
- `client/requirements.txt` - Add `cryptography` and `requests` dependencies
- `parent/api/main.py` - Add `GET /attestation` proxy endpoint
- `parent/api/schemas.py` - Add attestation response schema
- `parent/requirements.txt` - Add `httpx` dependency

### Module Design

**`LegalDocsClient`** - Client SDK for interacting with the LegalDocs pipeline.

Key functions:

- `__init__(api_url: str, ca_cert: str, client_cert: str = None, client_key: str = None)` - Configure API connection
- `get_attestation() -> dict` - Fetch attestation from `GET /attestation` on the parent API
- `verify_attestation(attestation: dict, ca_cert_path: str) -> bytes` - Verify signature against CA, return public key PEM
- `encrypt_document(plaintext: bytes, public_key_pem: bytes) -> bytes` - Envelope encrypt using enclave's public key (delegates to shared encrypt logic)
- `decrypt_result(encrypted_blob: bytes, public_key_pem: bytes) -> bytes` - For M2: decrypt results returned by the enclave (note: M1 scope only encrypts; result decryption uses the same envelope pattern but with a client key pair -- deferred to M2 design)

**Parent API `/attestation` proxy:**

- `GET /attestation` - Uses `httpx.AsyncClient` with mTLS certs to forward request to `https://{ENCLAVE_HOST}:{ENCLAVE_PORT}/attestation`, returns the JSON response unmodified

### Data Structures

```
AttestationPayload {
  enclave_id: str
  public_key: str
  timestamp: str
  platform: str
}

AttestationResponse {
  payload: AttestationPayload
  signature: str
  certificate: str
}
```

### Integration

- Client calls parent API `GET /attestation` (plain HTTP, port 8000)
- Parent API proxies to enclave `GET /attestation` (mTLS, port 8443)
- Client verifies attestation signature using the CA cert
- Client extracts public key from verified attestation
- Client uses public key for envelope encryption (same `encrypt_document` logic as `CryptoManager.encrypt_document`)
- The encrypt function should be shared or duplicated between `enclave/crypto.py` and `client/client.py` (duplicated is simpler since they are separate packages with separate dependencies)

### Success Criteria

**Automated Verification:**

```bash
# With docker compose up:
python -c "
from client.client import LegalDocsClient
c = LegalDocsClient('http://localhost:8000', ca_cert='certs/ca.crt')
att = c.get_attestation()
pub_key = c.verify_attestation(att, 'certs/ca.crt')
encrypted = c.encrypt_document(b'Test legal document', pub_key)
print(f'Encrypted blob size: {len(encrypted)} bytes')
print('Phase 3: PASS')
"
```

**Manual Verification:**

- [ ] `GET /attestation` on parent API returns the enclave's attestation JSON
- [ ] Client successfully verifies attestation signature
- [ ] Client extracts valid public key from attestation
- [ ] Client encrypts a document and produces a binary blob
- [ ] Parent API has no `cryptography` dependency (confirm it cannot decrypt)

---

## Phase 4: Integration Verification

### Goal

Verify the end-to-end flow: client encrypts a document, the parent API stores only ciphertext, the enclave decrypts it, and the parent never sees plaintext. This phase writes verification tests and a demo script.

### Files to Modify

- `tests/test_m1_encryption.py` - New file: unit tests for crypto module
- `tests/test_m1_attestation.py` - New file: unit tests for attestation
- `tests/test_m1_integration.py` - New file: integration tests proving privacy guarantee
- `scripts/verify_m1.py` - New file: standalone verification script

### Module Design

**Unit tests (`test_m1_encryption.py`):**

- `test_encrypt_decrypt_round_trip` - Basic encrypt/decrypt with CryptoManager
- `test_decrypt_wrong_key_fails` - Different key pair cannot decrypt
- `test_decrypt_tampered_ciphertext_fails` - Modified ciphertext raises InvalidTag
- `test_empty_document` - Edge case: empty bytes
- `test_large_document` - Edge case: 1MB document

**Unit tests (`test_m1_attestation.py`):**

- `test_attestation_format` - Attestation JSON has all required fields
- `test_attestation_signature_valid` - Signature verifies against certificate
- `test_attestation_public_key_matches` - Public key in attestation matches CryptoManager's key

**Integration tests (`test_m1_integration.py`):**

- `test_client_encrypt_enclave_decrypt` - Client encrypts, enclave's `/decrypt` endpoint decrypts
- `test_parent_cannot_decrypt` - Parent API does not have the private key; encrypted blob is opaque
- `test_attestation_proxy` - Parent API correctly proxies attestation from enclave
- `test_enclave_isolation` - Enclave cannot reach external network or database

**Verification script (`scripts/verify_m1.py`):**

- Runs with `docker compose up`
- Fetches attestation via parent API
- Verifies attestation
- Encrypts a sample document
- Sends encrypted blob to enclave's `/decrypt` via parent (or directly for M1)
- Confirms decrypted result matches original
- Confirms parent process memory/logs contain no plaintext

### Integration

- Unit tests run without Docker (pure Python, `pytest`)
- Integration tests require `docker compose up`
- Verification script is a standalone demo

### Success Criteria

**Automated Verification:**

```bash
# Unit tests (no Docker needed):
pytest tests/test_m1_encryption.py tests/test_m1_attestation.py -v

# Integration tests (Docker required):
docker compose up -d
pytest tests/test_m1_integration.py -v

# Full verification:
docker compose up -d
python scripts/verify_m1.py
```

**Manual Verification:**

- [ ] All unit tests pass
- [ ] All integration tests pass
- [ ] Verification script prints end-to-end success
- [ ] Inspecting parent API container logs shows no plaintext
- [ ] Enclave container cannot `ping` external hosts (network isolation)
- [ ] Enclave container cannot connect to postgres on port 5432

---

## Rollback Plan

All changes are additive. Existing M0 functionality (`/health` endpoints, Docker Compose setup, DB schema) is not modified in breaking ways. To rollback:

1. Revert commits on the branch
2. `docker compose down && docker compose build && docker compose up`

The enclave's `/health` endpoint remains functional throughout all phases.

## Open Questions

1. **Result encryption in M2**: The current M1 scope encrypts documents client-to-enclave. For M2, the enclave needs to encrypt extraction results back to the client. This likely requires the client to also send a public key, or the enclave re-encrypts results with the same document DEK. Deferred to M2 planning.
2. **Key size configurability**: RSA-2048 is hardcoded for now. Adding `ENCLAVE_RSA_KEY_SIZE` env var is trivial but not essential for M1.
3. **Attestation caching**: The client could cache the attestation/public key. Not needed for M1 (single-use flows), but relevant for M2 when the client sends multiple documents.
