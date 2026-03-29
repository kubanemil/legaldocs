# Enclave + Encryption - PR FAQ

> **Milestone**: M1
> **Created**: 2026-03-29
> **Status**: Draft

---

## Press Release

### Enclave + Encryption

**For** developers and organizations processing sensitive legal documents
**Who** need to guarantee that documents remain private throughout the processing pipeline -- invisible to the API server, infrastructure operators, and anyone except the document owner
**The** Enclave + Encryption system
**Is a** new core capability of the legal document processing pipeline
**That** ensures documents are encrypted on the client before they ever leave the user's machine, and can only be decrypted inside a verified, isolated enclave container
**Unlike** traditional document processing systems where the API server handles plaintext and privacy depends on access controls and trust
**Our solution** enforces privacy architecturally -- the parent API physically cannot read documents because it never holds the decryption key. Only a verified enclave, running in an isolated container with no internet and no database access, can decrypt and process documents.

---

Today we are announcing the foundational privacy layer of the legal document processing pipeline: Enclave + Encryption.

When a client wants to process a document, it first requests an attestation from the enclave -- a cryptographic proof that the enclave is genuine and running in an isolated environment. The attestation includes the enclave's public key. The client verifies this attestation, then encrypts the document using that public key before sending it to the API. From that point forward, the API server, the database, the task queue, and every other component in the system only ever sees ciphertext. The document can only be decrypted inside the enclave container itself, which runs on a read-only filesystem with no internet access, no database connection, and only tmpfs for temporary storage. This mirrors the AWS Nitro Enclave pattern and demonstrates that true end-to-end document privacy is achievable without requiring users to trust the platform operator.

---

## FAQ

### Customer Experience

Q: How does a client discover and use this feature?
A: The client calls `GET /attestation` to receive the enclave's attestation document containing its public key. After verifying the attestation, the client uses the public key to encrypt documents locally before uploading them via the API. A client SDK handles this flow transparently.

Q: What does the happy path look like step-by-step?
A: (1) Client requests attestation from enclave via the parent API. (2) Client verifies the attestation document. (3) Client encrypts the document using the enclave's public key (RSA/AES hybrid encryption). (4) Client sends the ciphertext to the API. (5) The enclave later decrypts the ciphertext using its private key for processing. The parent API never sees plaintext at any step.

Q: What happens if the enclave restarts?
A: The enclave generates a fresh key pair on each startup. Clients must request a new attestation and use the new public key. Documents encrypted with a previous key pair cannot be decrypted by the new enclave instance. This is expected behavior -- in production, key persistence would be handled by KMS integration (out of scope for this milestone).

Q: How does the client know it is talking to a legitimate enclave?
A: The enclave returns a simulated attestation document that includes signed metadata and a certificate. The client verifies this certificate to confirm the enclave's identity. In production, this would be hardware-backed Nitro attestation; here it is simulated with self-signed certificates over mTLS.

### Scope & Non-Goals

Q: What is explicitly NOT included in V1?
A: This milestone does NOT include: document processing/extraction (M2), the Celery pipeline for forwarding ciphertext to the enclave (M2), API endpoints for uploading or retrieving documents (M2), tenant isolation (M3), persistent key storage or KMS integration, hardware-based attestation, or production-grade certificate management.

Q: What is the minimal version that still delivers the core benefit?
A: An enclave container that generates a key pair, exposes an attestation endpoint over mTLS, and can decrypt documents encrypted with its public key. Plus a client that can request attestation, verify it, and encrypt documents. This proves the core privacy guarantee works end-to-end.

Q: What are we deferring to future iterations?
A: Key rotation, key persistence across restarts (KMS), real hardware attestation, certificate rotation, and integration with the processing pipeline (M2). The enclave in M1 only proves it can decrypt -- it does not yet extract fields or return results.

### Technical Constraint FAQs

Q: What existing system capabilities does this depend on?
A: This depends on M0 deliverables: Docker Compose running all services, the mTLS certificate generation script, and the basic enclave container setup with internal-only networking.

Q: What new capabilities need to be built?
A: (1) Enclave-side key pair generation on startup. (2) Attestation endpoint serving the public key and signed metadata over mTLS. (3) Client-side attestation verification. (4) Client-side hybrid RSA/AES encryption using the enclave's public key. (5) Enclave-side decryption of hybrid-encrypted payloads. (6) Docker configuration enforcing enclave isolation (no internet, no DB, read-only root, tmpfs).

Q: What is the simplest implementation that delivers the PR outcome?
A: The enclave generates an RSA key pair in memory on startup and serves a `/attestation` endpoint over mTLS that returns the public key plus a self-signed attestation blob. The client SDK has functions to request attestation, verify the certificate, encrypt a document with hybrid RSA/AES, and the enclave has a corresponding decrypt function. The enclave Docker container is configured with `read_only: true`, `tmpfs: [/tmp]`, and attached only to the internal Docker network.

### Edge Cases & Error States FAQs

Q: What happens if the client tries to encrypt without first requesting attestation?
A: The client SDK should require an attestation step before encryption. Without the enclave's public key, encryption cannot proceed. The SDK raises an error if encryption is attempted without a verified public key.

Q: What happens if the mTLS connection to the enclave fails?
A: The attestation request fails with a connection error. The client cannot proceed to encrypt because it has no public key. This is a safe failure -- no plaintext is ever sent unencrypted.

Q: What happens if an encrypted document is sent to the wrong enclave instance (different key pair)?
A: Decryption fails. The enclave returns a decryption error. The document remains confidential because only the original enclave instance's private key could decrypt it.

Q: What happens if the enclave container's isolation is misconfigured?
A: M1 should include a verification check (or test) that confirms: the enclave cannot reach external networks, cannot access the database, and has a read-only root filesystem. If any check fails, it indicates a configuration problem that must be fixed before the privacy guarantee holds.

### Business & Operational FAQs

Q: How will we know if this is successful?
A: Success means: (1) A client can encrypt a document that the parent API demonstrably cannot decrypt. (2) The enclave can decrypt that same document. (3) The enclave container passes isolation checks (no internet, no DB, read-only fs). These are all verifiable with automated tests.

Q: What could go wrong post-launch?
A: Key pair loss on enclave restart (expected, documented). mTLS certificate expiration (mitigated by regeneration script). Misconfigured Docker networking that breaks isolation (mitigated by isolation verification tests).

Q: Are there compliance or security considerations?
A: This milestone uses the `cryptography` Python library (not hand-rolled crypto). The simulated attestation is not a real security boundary -- it demonstrates the pattern. Production deployment would require real Nitro attestation and AWS KMS.

---

## Implementation Scope

### Current State -> End State

| Current State | End State |
| --- | --- |
| Enclave container exists but has no encryption capability | Enclave generates RSA key pair on startup and can decrypt documents |
| No attestation mechanism exists | Enclave exposes an attestation endpoint over mTLS returning its public key and signed metadata |
| Client has no way to verify the enclave or encrypt documents | Client can request attestation, verify it, and encrypt documents with the enclave's public key |
| Enclave container isolation is not fully enforced | Enclave runs with no internet, no DB access, read-only filesystem, tmpfs only |
| Parent API could theoretically access plaintext | Parent API is architecturally excluded from plaintext -- it never holds the private key |

### User Stories

- [ ] As the enclave, I generate a key pair on startup and expose an attestation endpoint over mTLS (M1.1)
- [ ] As a client, I can request and verify the enclave's attestation and obtain its public key (M1.2)
- [ ] As a client, I can encrypt a document using the enclave's public key before uploading (M1.3)
- [ ] As the enclave, I can decrypt a document that was encrypted with my public key (M1.4)
- [ ] As the enclave, I run in a Docker container with no internet, no DB access, read-only filesystem, tmpfs only (M1.5)

### Success Criteria

- [ ] Enclave generates an RSA key pair on startup and the public key is accessible via the attestation endpoint
- [ ] Client can retrieve and verify the attestation document over mTLS
- [ ] Client can encrypt a sample document using the enclave's public key (hybrid RSA/AES)
- [ ] Enclave can decrypt a document encrypted by the client and recover the original plaintext
- [ ] Parent container demonstrably cannot decrypt documents (does not hold the private key)
- [ ] Enclave container cannot reach external networks (verified by test)
- [ ] Enclave container cannot access the database (verified by test)
- [ ] Enclave container filesystem is read-only with tmpfs for /tmp

### Known Compromises

- [ ] Attestation is simulated with self-signed certificates, not hardware-backed -- acceptable for local MVP, production would use Nitro attestation
- [ ] Key pairs are ephemeral (lost on restart) -- acceptable because KMS integration is out of scope; documents from previous sessions cannot be decrypted after restart
- [ ] RSA/AES hybrid encryption is used for simplicity -- production might use different envelope encryption via KMS
- [ ] mTLS certificates are self-signed and manually generated -- acceptable for local development

### Open Questions / Needs Research

- [ ] What RSA key size should we use? (2048 vs 4096 -- balance between startup speed and security simulation)
- [ ] What format should the attestation document use? (JSON blob with public key + metadata + signature?)
- [ ] Should the enclave expose a separate `/decrypt` endpoint in M1, or is decrypt tested only as a library function for M2 to integrate with?
- [ ] How exactly should the Docker network isolation be configured in docker-compose.yml to ensure the enclave has zero external access?

---

## Next Steps

When ready to implement:

1. Run `/pipe-research --project legaldocs --ref m1 enclave encryption` to analyze codebase
2. Run `/pipe-plan-implementation --project legaldocs --ref m1 enclave encryption` to create implementation plan
3. Run `/pipe-implement docs/dev/legaldocs/m1-enclave-encryption/implementation-plan.md` to execute the plan
