---
date: 2026-03-29
researcher: claude-opus-4-6
branch: main
repository: legaldocs
tags: encryption, rsa, aes, envelope-encryption, enclave, attestation, mtls, cryptography
status: complete
---

# Research: M1 Enclave + Encryption

## Research Question

How to implement RSA/AES hybrid (envelope) encryption for the enclave, including key generation, simulated attestation, client-side encryption, enclave-side decryption, and integration with the existing mTLS server -- all using Python's `cryptography` library.

## Summary

M1 requires building the encryption layer that makes privacy guarantees enforceable. The enclave generates an RSA key pair on startup, serves a simulated attestation endpoint over the existing mTLS server, and exposes a decrypt capability. The client fetches the attestation, verifies it, encrypts documents using hybrid RSA/AES envelope encryption, and sends ciphertext to the parent API. The parent API passes encrypted blobs through without ever decrypting them.

The `cryptography` library (already in `enclave/requirements.txt`) provides all needed primitives: RSA key generation, OAEP padding for asymmetric encryption, AES-GCM for symmetric encryption, and X.509 certificate utilities for attestation signing. The implementation pattern is straightforward envelope encryption: generate a random AES-256 key per document, encrypt the document with AES-GCM, encrypt the AES key with the enclave's RSA public key using OAEP, and bundle them together. The enclave reverses this: decrypt the AES key with its RSA private key, then decrypt the document with the recovered AES key.

The existing M0 codebase provides: an mTLS server in `enclave/server.py` (lines 1-55) with a `/health` endpoint, placeholder files for `enclave/crypto.py` and `client/client.py`, a Docker Compose setup with the enclave on an internal-only network with read-only filesystem, and mTLS certificate generation via `scripts/generate_certs.sh`.

## Detailed Findings

### 1. RSA Key Pair Generation in the Enclave on Startup

The enclave needs to generate an RSA key pair in memory when it starts. This is separate from the mTLS certificates (which authenticate the transport layer). The RSA key pair is for document encryption/decryption.

**Implementation with `cryptography`:**

```python
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

# Generate RSA-4096 key pair (or RSA-2048 for faster startup)
private_key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=4096,  # or 2048 for speed in MVP
)
public_key = private_key.public_key()

# Serialize public key to PEM for transmission
public_key_pem = public_key.public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
```

**Key size decision:** RSA-2048 generates in ~0.1s, RSA-4096 in ~1-2s. For the MVP, RSA-2048 is sufficient and faster. The press release FAQ leaves this open. RSA-2048 is the pragmatic choice for a local simulation.

**Lifecycle:** The private key lives only in memory. On container restart, a new key pair is generated. Documents encrypted with the old public key become unrecoverable. This is documented as expected behavior (KMS persistence is out of scope).

**Where to put it:** `enclave/crypto.py` -- currently a placeholder (line 1-4). The key pair should be generated once at module load or in a startup function called before the server starts listening.

### 2. Simulated Attestation Endpoint

The attestation endpoint returns the enclave's public key plus metadata, simulating what AWS Nitro attestation would provide. It should be served over the existing mTLS server.

**Attestation document format (JSON):**

```json
{
    "enclave_id": "<uuid>",
    "public_key": "<PEM-encoded RSA public key>",
    "timestamp": "<ISO 8601>",
    "platform": "docker-simulation",
    "pcr0": "<sha256 hash of enclave image -- simulated>",
    "signature": "<base64-encoded signature of the above fields>"
}
```

**Signing the attestation:** The attestation document should be signed with the enclave's mTLS private key (the TLS identity key at `/certs/enclave.key`). This simulates how Nitro attestation is signed by the Nitro hypervisor. The client can verify the signature using the CA certificate it already trusts.

```python
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# Load mTLS key for signing attestation
with open("/certs/enclave.key", "rb") as f:
    signing_key = load_pem_private_key(f.read(), password=None)

# Sign the attestation payload (JSON bytes minus the signature field)
signature = signing_key.sign(
    payload_bytes,
    asym_padding.PKCS1v15(),
    hashes.SHA256(),
)
```

**Integration with existing server:** The current `enclave/server.py` has an `EnclaveHandler` class with `do_GET` handling `/health`. A new route `/attestation` should be added in the same handler. The handler class would need access to the crypto module's key pair and signing logic.

**Relevant existing code:**
- `enclave/server.py:26-38` -- `EnclaveHandler.do_GET` currently handles only `/health`
- `enclave/server.py:45-51` -- `run_server` starts the HTTPServer with SSL context
- The SSL context at `enclave/server.py:13-22` already loads the mTLS certs

### 3. Client-Side Encryption (Envelope Encryption)

The client encrypts documents using hybrid RSA/AES encryption. This is the standard envelope encryption pattern.

**Steps:**
1. Generate a random 256-bit AES key (the Data Encryption Key, DEK)
2. Generate a random 96-bit (12-byte) IV/nonce for AES-GCM
3. Encrypt the document plaintext with AES-256-GCM using the DEK and nonce
4. Encrypt the DEK with the enclave's RSA public key using OAEP padding
5. Bundle: encrypted_key + nonce + ciphertext + GCM auth tag

**Implementation with `cryptography`:**

```python
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives import hashes, serialization

def encrypt_document(plaintext: bytes, public_key_pem: bytes) -> bytes:
    """Encrypt document using envelope encryption."""
    # Load the enclave's public key
    public_key = serialization.load_pem_public_key(public_key_pem)

    # Generate random AES-256 key (DEK)
    dek = os.urandom(32)  # 256 bits

    # Generate random nonce for AES-GCM
    nonce = os.urandom(12)  # 96 bits

    # Encrypt plaintext with AES-GCM
    aesgcm = AESGCM(dek)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    # ciphertext includes the 16-byte GCM auth tag appended

    # Encrypt the DEK with RSA-OAEP
    encrypted_dek = public_key.encrypt(
        dek,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # Bundle: [4 bytes: len(encrypted_dek)] + encrypted_dek + nonce + ciphertext
    encrypted_dek_len = len(encrypted_dek).to_bytes(4, byteorder="big")
    return encrypted_dek_len + encrypted_dek + nonce + ciphertext
```

**Wire format:** A simple binary format with a length-prefixed encrypted DEK, followed by the fixed-size nonce, followed by the AES-GCM ciphertext (which includes the auth tag). The encrypted DEK length varies by RSA key size (256 bytes for RSA-2048, 512 bytes for RSA-4096), so a length prefix is needed.

**Where to put it:** `client/client.py` -- currently a placeholder (lines 1-4). The `cryptography` library must be added to `client/requirements.txt` (currently empty except a comment).

### 4. Enclave-Side Decryption

The enclave reverses the envelope encryption using its private key.

**Implementation with `cryptography`:**

```python
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives import hashes

def decrypt_document(encrypted_blob: bytes, private_key) -> bytes:
    """Decrypt an envelope-encrypted document."""
    # Parse the wire format
    encrypted_dek_len = int.from_bytes(encrypted_blob[:4], byteorder="big")
    offset = 4
    encrypted_dek = encrypted_blob[offset : offset + encrypted_dek_len]
    offset += encrypted_dek_len
    nonce = encrypted_blob[offset : offset + 12]
    offset += 12
    ciphertext = encrypted_blob[offset:]

    # Decrypt the DEK with RSA-OAEP
    dek = private_key.decrypt(
        encrypted_dek,
        asym_padding.OAEP(
            mgf=asym_padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    # Decrypt the document with AES-GCM
    aesgcm = AESGCM(dek)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext
```

**Where to put it:** `enclave/crypto.py` alongside the key generation code. The decrypt function needs access to the enclave's private key (generated on startup).

**Error handling:** `decrypt` raises `cryptography.exceptions.InvalidTag` if the ciphertext was tampered with or the wrong key was used. `private_key.decrypt` raises `ValueError` if the encrypted DEK is invalid. Both should be caught and returned as structured error responses.

### 5. Parent API Pass-Through Behavior

The parent API handles encrypted blobs without ever decrypting them. This is already architecturally set up in M0.

**Current state:**
- `parent/models/models.py:29` -- `encrypted_content = Column(LargeBinary, nullable=False)` stores the encrypted document blob
- `parent/models/models.py:30` -- `encrypted_result = Column(LargeBinary, nullable=True)` stores the encrypted extraction result
- `parent/models/models.py:69` -- `encrypted_value = Column(LargeBinary, nullable=False)` for extracted field values
- `parent/api/main.py:1-10` -- Currently only has `/health`. The API endpoints for document upload/retrieval are M2 scope.

**M1 impact on parent:** The parent API needs a new `/attestation` endpoint that proxies the request to the enclave over mTLS and returns the attestation document to the client. The parent does NOT need any encryption/decryption logic. It just forwards bytes.

```
Client -> GET /attestation -> Parent API -> mTLS GET /attestation -> Enclave
Client <- attestation JSON <- Parent API <- attestation JSON <- Enclave
```

**Key point:** The parent API container does NOT have `cryptography` in its requirements and should not need it. The parent never encrypts or decrypts. It is a pass-through for the attestation endpoint and (in M2) for encrypted document blobs.

### 6. Integration with Existing mTLS Server

The enclave's `server.py` already has a working mTLS HTTP server. M1 additions must integrate cleanly.

**Current server architecture:**
- `enclave/server.py:13-22` -- `create_ssl_context()` creates the mTLS SSL context using certs from environment variables
- `enclave/server.py:26-38` -- `EnclaveHandler` extends `BaseHTTPRequestHandler` with `/health` route
- `enclave/server.py:45-51` -- `run_server()` wraps the HTTPServer socket with SSL

**Integration approach for M1:**
1. Import the crypto module in `server.py`
2. Initialize the key pair before starting the server (in `run_server()` or at module level)
3. Add `/attestation` route in `EnclaveHandler.do_GET`
4. Add `/decrypt` route in `EnclaveHandler.do_POST` (optional for M1 -- could be a library function only)

**Whether to add `/decrypt` endpoint in M1:** The press release FAQ (line 136) asks this as an open question. The argument for adding it: M2 needs to call the enclave to decrypt, and the transport mechanism is mTLS HTTP. Adding the endpoint in M1 makes it testable end-to-end. The argument against: M1 scope is encryption primitives, M2 is the pipeline. Recommendation: add a `/decrypt` endpoint in M1 as it validates the full round-trip, even though M2 will be the one that calls it from the Celery worker.

**Handler needs access to crypto state:** The `BaseHTTPRequestHandler` subclass does not easily support dependency injection. Options:
- Module-level global for the key pair (simplest)
- Custom server class that holds state and is accessible via `self.server`
- The `HTTPServer` subclass pattern: create a custom `EnclaveHTTPServer(HTTPServer)` with a `crypto` attribute

The custom server class approach is cleaner:

```python
class EnclaveHTTPServer(HTTPServer):
    def __init__(self, server_address, handler_class, crypto_manager):
        super().__init__(server_address, handler_class)
        self.crypto = crypto_manager
```

Then in the handler: `self.server.crypto.private_key`, `self.server.crypto.get_attestation()`.

### 7. Docker Compose and Enclave Isolation

**Current state** (`docker-compose.yml`):
- `enclave` service (lines 92-119): already on `enclave-net` (internal-only), `read_only: true`, `tmpfs: [/tmp]`
- The enclave is NOT on `external-net`, so it cannot reach postgres, redis, or the internet
- The healthcheck (lines 107-112) uses an inline Python script with mTLS to check `/health`

**M1 changes needed:**
- No network changes required -- isolation is already configured
- The enclave Dockerfile (`enclave/Dockerfile`) may need updating if crypto module imports new dependencies, but `cryptography` is already in `enclave/requirements.txt`
- The healthcheck may need updating if the server startup takes longer due to key generation

### 8. Attestation Document Format Decision

**Format options considered:**

Option A: Simple JSON with embedded signature
```json
{
    "payload": {
        "enclave_id": "...",
        "public_key": "-----BEGIN PUBLIC KEY-----\n...",
        "timestamp": "2026-03-29T12:00:00Z",
        "platform": "docker-simulation"
    },
    "signature": "<base64>",
    "certificate": "<PEM of enclave's mTLS cert>"
}
```

Option B: JWT-style (JSON Web Signature)
- More standard but adds a dependency (PyJWT or manual JWS construction)

**Recommendation:** Option A is simpler and sufficient for a simulation. The client verifies by: (1) checking the certificate chains to the trusted CA, (2) verifying the signature over the payload bytes using the certificate's public key, (3) extracting the enclave's encryption public key from the payload.

### 9. Dependencies

**Enclave (`enclave/requirements.txt`):**
- `cryptography` -- already listed (line 1). No additional dependencies needed.

**Client (`client/requirements.txt`):**
- `cryptography` -- must be added. Needed for RSA public key loading, AES-GCM encryption, OAEP encryption, and attestation verification.
- `requests` -- for making HTTP calls to the parent API. May also be useful for direct mTLS calls during testing.

**Parent (`parent/requirements.txt`):**
- `httpx` or `requests` -- needed for the parent to proxy the `/attestation` request to the enclave over mTLS. `httpx` is preferable as it has async support matching FastAPI. Currently not listed.

## Code References

- `enclave/server.py:1-55` -- Current mTLS HTTP server with `/health` endpoint
- `enclave/server.py:13-22` -- SSL context creation using environment variable cert paths
- `enclave/server.py:26-38` -- `EnclaveHandler` request handler class
- `enclave/crypto.py:1-4` -- Placeholder for crypto operations
- `client/client.py:1-4` -- Placeholder for client SDK
- `parent/api/main.py:1-10` -- Current FastAPI app with only `/health`
- `parent/models/models.py:29-30` -- `encrypted_content` and `encrypted_result` LargeBinary columns
- `parent/models/models.py:69` -- `encrypted_value` LargeBinary column for extracted fields
- `docker-compose.yml:92-119` -- Enclave service config with isolation (internal network, read-only, tmpfs)
- `docker-compose.yml:114-119` -- Network definitions (`external-net` bridge, `enclave-net` internal bridge)
- `scripts/generate_certs.sh:1-59` -- mTLS cert generation (CA, enclave, parent certs)
- `enclave/requirements.txt:1` -- `cryptography` already listed
- `client/requirements.txt:1` -- Empty placeholder
- `parent/requirements.txt:1-8` -- No HTTP client library for proxying to enclave
- `enclave/Dockerfile:1-9` -- Simple Python 3.12-slim image, copies enclave code

## Architecture Notes

The M1 encryption layer sits between the client and the enclave, with the parent API as an opaque pass-through. The architecture has three trust domains:

1. **Client (trusted)** -- Has the enclave's public key (via attestation). Encrypts before sending. Decrypts results after receiving. Never sends plaintext over the network.

2. **Parent API + Infrastructure (untrusted)** -- Handles only ciphertext. Cannot decrypt because it never holds the RSA private key. The database stores only encrypted blobs. Redis queues carry only document IDs.

3. **Enclave (trusted, verified)** -- Holds the RSA private key in memory only. Generates key pair on startup. Decrypts documents, processes them, encrypts results. Runs in an isolated container with no external network access.

The mTLS layer (M0) authenticates transport between parent and enclave. The RSA/AES layer (M1) protects document content end-to-end from client to enclave. These are independent security layers -- mTLS ensures only the parent can talk to the enclave; envelope encryption ensures only the enclave can read documents.

## Open Questions

1. **RSA key size:** RSA-2048 is recommended for the MVP (faster startup, sufficient for simulation). RSA-4096 can be made configurable via environment variable if desired.

2. **`/decrypt` endpoint in M1 vs M2:** Recommended to add in M1 to enable end-to-end testing. The endpoint accepts an encrypted blob and returns the decrypted plaintext (over mTLS, so transport is secure). M2 will call this endpoint from the Celery worker.

3. **Attestation format:** Simple JSON with payload + signature + certificate is recommended. No need for JWT complexity in the MVP.

4. **Parent proxying attestation:** The parent API needs an HTTP client library (recommend `httpx` for async compatibility with FastAPI) to forward `/attestation` requests to the enclave. This is a new dependency for the parent.

5. **Key pair held as module global vs server attribute:** The custom `HTTPServer` subclass pattern is cleaner but either works for the MVP. The custom server approach avoids polluting the module namespace and makes testing easier.

---

## Next Steps

If you are satisfied with this research, proceed to planning:

/pipe-plan-implementation --project legaldocs --ref m1 --research docs/dev/legaldocs/m1-enclave-encryption/research.md enclave encryption

If you need more research:

- Ask follow-up questions and I will append to this document
- Or request a new research topic with /pipe-research --project legaldocs --ref m1 [new-topic]
