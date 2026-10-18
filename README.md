# legaldocs

legaldocs pulls parties and dates out of legal documents while the server that stores and
queues them cannot read them. Before sending anything, the client checks an attestation of the
enclave's code. It then seals each document to a key only that enclave holds, and gets the
result back sealed to a key it made for that one request. The enclave signs a receipt for every
document, and the receipt's hash is timestamped on Ethereum. Everything runs on one machine
with Docker: a container stands in for the AWS Nitro enclave, and a mock security module stands
in for Nitro's hardware attestation.

## Threat model

The adversary is the parent: the API, the worker, Postgres and Redis. It can read and change
everything it stores, replay and reorder messages, and run any code in its own containers.

What the simulation guarantees against it:

- The parent never holds a key that opens a document or a result. Documents are sealed to the
  enclave's key. Results are sealed to a key the client made for that request, and that key
  travels inside the sealed document, so the parent cannot swap in its own.
- The parent cannot pass off its own key as the enclave's. Only the mock NSM can sign an
  attestation. Its key lives in a container with no network, and the parent can reach neither
  that key nor the socket the enclave uses to ask for signatures.
- A recorded attestation cannot be replayed and a different enclave build is refused. Every
  attestation carries the client's nonce, PCR0 and a timestamp, and the client checks all three.
- Every ciphertext is bound to its tenant, its document id and the protocol version. A
  document moved to another tenant, or a result swapped between documents, fails to decrypt.
- A receipt commits to the input and the output under a salt only the client knows. It proves
  what was processed, and neither the receipt nor its on-chain hash lets anyone confirm a
  guessed document.

What it does not guarantee:

- Memory isolation. The enclave is an ordinary container, so root on the host can read its
  memory, swap its image or run a different NSM. A Nitro enclave closes that door. This repo
  shows the protocol, not the isolation.
- A hardware root of trust. The NSM is software that makes its root key at boot, and the client
  copies the root certificate off the host (`./do dev` does it). With Nitro, the root is AWS's,
  pinned once.
- Code identity of the running bytes. PCR0 here hashes the enclave's build inputs: a Dockerfile
  with a digest-pinned base, hash-locked requirements and the source. The NSM measures a copy of
  those inputs baked into its own image, so a host that builds the two images from different
  trees defeats the check.
- Availability. The parent can drop, delay or refuse any request.

The enclave does see plaintext, since that is its job. Its logs leave the enclave, so they
carry request lines and error types, never document content.

## Architecture

```
client SDK ──HTTPS + API key──▶ api ─────┬── postgres  ciphertext, receipts, hashed API keys
(plaintext lives here)                   ├── redis     task ids only
                               worker ───┴── anvil     ReceiptRegistry: receipt hashes
                                 │
                   mTLS on an internal network (api, worker, enclave only),
                   enclave certificate pinned by attestation
                                 │
                              enclave ──Unix socket──▶ mock NSM
                   keys made at boot, read-only,       no network; the only key
                   no host volumes but the parent CA   that signs attestations
```

A request, with the attestation handshake first:

1. The client sends a fresh 32-byte nonce to `GET /attestation`. The parent passes it to the
   enclave, which asks the NSM for a document carrying the nonce, the enclave's encryption key,
   its receipt-signing key and the hash of its TLS certificate. The NSM adds the PCRs it
   measured and signs the document with ES384.
2. The client checks the certificate chain up to the NSM root it pinned, the signature, the
   nonce, PCR0 and that the document is under five minutes old. Nothing leaves the client
   before this passes.
3. The client makes a one-off X25519 key pair and a random salt, then seals
   `{document, reply key, salt}` to the attested key with HPKE. Tenant, document id and
   version go into the HPKE context, so they are authenticated but not secret.
4. `POST /documents` stores the envelope. Only the ids go through Redis.
5. The worker sends the envelope to the enclave. The enclave opens it, extracts the fields,
   seals the result to the reply key and signs a receipt: binding hash, salted input and output
   hashes, PCR0 and time.
6. The worker stores both and anchors SHA-256 of the receipt on `ReceiptRegistry`.
7. The client opens the result, checks the receipt against what it sent, and can read the
   anchor's timestamp straight from the chain.

The parent reaches the enclave the same way. The enclave's TLS certificate is self-signed, and
the parent trusts it only after a fresh attestation binds its hash to the expected PCR0. The
enclave replaces the certificate halfway through its 24-hour life, and the parent re-attests
when the pinned one stops verifying.

Extraction runs in a small pool of worker processes. A document that takes longer than the
deadline (10 seconds by default) fails with `processing_timeout`, and its worker is killed and
replaced, so one tenant cannot hold the enclave for everyone.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/attestation?nonce=` | the enclave's attestation document |
| `POST` | `/documents` | `202`, after storing a sealed envelope under a client-chosen id |
| `GET` | `/documents`, `/documents/{id}` | status, sealed result, receipt, anchor tx |
| `GET` | `/tenant` | the tenant the API key belongs to |
| `GET` | `/health` | liveness, no auth |

Every route but `/health` takes `Authorization: Bearer <api key>`. There is no way to ask the
parent to decrypt anything.

### Key lifetime

The enclave makes its keys at boot and never writes them anywhere, so a restart re-keys.
Documents sealed to the old key can no longer be opened. The worker marks them `FAILED` with
`enclave_key_rotated`, and the SDK's `process()` re-attests and resubmits once. A restart costs
the documents in flight. In return there is no sealed key on disk to steal and no key-release
service to trust. Milestone 2 removes the cost with KMS.

## Layout

```
protocol/    wire formats every party shares: HPKE envelopes, COSE_Sign1, attestation, receipts
nsm/         mock Nitro Security Module: measures the enclave, signs attestations
enclave/     the enclave: boot-time keys, attested TLS, bounded HTTPS server, extraction
parent/      API, Celery worker, attested enclave link, chain relayer, admin CLI
client/      SDK: attest, seal, submit, open, verify
contracts/   ReceiptRegistry and its Foundry tests
alembic/     database migrations
scripts/     parent certificates, demo, contract artifact export
tests/       unit tests without Docker; tests/smoke runs against the compose stack
```

## Development

You need Docker with Compose, [uv](https://docs.astral.sh/uv/) and
[Foundry](https://getfoundry.sh).

```
./do test     # unit tests and Foundry tests, no Docker
./do check    # ruff, mypy, forge fmt, lockfile, image requirements, contract artifact
./do dev      # issue certs if needed, measure the enclave, build and start the stack
./do demo     # two tenants run three contracts through the running stack
./do smoke    # fresh stack, end-to-end and privacy checks, teardown
./do down     # stop the stack and delete its volumes
```

`./do smoke` fails if a test document shows up in a Postgres dump, an uncompressed Redis
dump or any container log, if the enclave can reach the internet, Postgres, Redis, anvil or
the NSM over the network, or if a host key reaches the enclave. CI runs it on every pull
request.

`./do key NAME` creates a tenant on the running stack and prints an API key. `./do pcr0` prints
the PCR0 a client should expect from this checkout.

## Configuration

The parent (api and worker) reads:

| Variable | Meaning |
|---|---|
| `DATABASE_URL`, `REDIS_URL` | Postgres and Redis |
| `ENCLAVE_HOST`, `ENCLAVE_PORT` | where the enclave listens |
| `PARENT_CERT`, `PARENT_KEY` | the parent's mTLS identity, from `./do certs` |
| `NSM_TRUST_ANCHOR` | the NSM root certificate |
| `EXPECTED_PCR0` | the enclave build the parent will talk to; `./do dev` sets it |
| `ETH_RPC_URL`, `REGISTRY_ADDRESS` | the chain and the one registry to use |
| `REGISTRY_DEPLOY_BLOCK` | the registry's deploy block; log scans never go below it |
| `RELAYER_KEY` | worker only: the key allowed to anchor |
| `MAX_ENVELOPE_BYTES` | largest accepted envelope, 4 MiB by default |

The enclave reads `PARENT_CA`, `NSM_SOCKET`, `PORT`, `MAX_CONNECTIONS`, `MAX_BODY_BYTES`,
`EXTRACT_WORKERS` (2) and `PROCESS_DEADLINE_SECONDS` (10).
The NSM reads `NSM_SOCKET`, `NSM_TRUST_ANCHOR` and `NSM_MEASURED_DIR`. `./do` publishes the API
on `API_PORT` (8000) and anvil on `ANVIL_PORT` (8545).

The compose file uses anvil's public development keys. The `deploy` service deploys the
registry once, and refuses to send the deployment if it would land anywhere but
`REGISTRY_ADDRESS`. Workers refuse to start if nothing is deployed there.

## Decisions

| Choice | Not chosen | Why |
|---|---|---|
| HPKE (RFC 9180) from `cryptography`: X25519, HKDF-SHA256, AES-256-GCM | pyhpke; X25519 + HKDF + AES-GCM by hand | `cryptography` 50 ships HPKE, so there is no extra dependency and no hand-assembled scheme. Its API is single-shot and takes `info` only. The binding goes there, which authenticates it exactly as AAD would in single-shot mode. |
| Nitro's attestation format: COSE_Sign1, ES384, the same CBOR fields | A custom signed JSON blob | The verifier runs unchanged on real NSM documents. Milestone 2 swaps the trust anchor and the NSM client, nothing else. |
| COSE_Sign1 framed with `cbor2`, signed with `cryptography` | pycose | Sign1 is a four-element CBOR array. pycose would add `ecdsa` and `attrs` for that. |
| NSM in its own container, no network, socket shared only with the enclave | NSM inside the enclave container | The attestation key stays away from both the enclave and the parent, as with the hypervisor. |
| Attested TLS: the enclave self-signs, the parent pins what the attestation binds | An enclave CA on the host | Any CA key on the host lets the parent mint an enclave certificate. |
| Parent CA used once for a 7-day `clientAuth` leaf, then discarded | A long-lived CA with revocation lists | Nothing on disk can mint another parent certificate. Short lifetime stands in for revocation. |
| Re-key on restart with a defined failure | Sealing the key to disk | See [Key lifetime](#key-lifetime). |
| Per-tenant API keys, stored as SHA-256 of a 256-bit secret | JWTs; bcrypt or argon2 | Opaque keys revoke instantly and need no signing key. Slow hashes protect guessable passwords, and these secrets are random. |
| `(tenant_id, id)` as the document primary key | Global document ids | Every lookup needs the tenant, and a tenant cannot learn that another used an id. |
| Only the receipt hash on chain | Document id or tenant id on chain | The hash reveals nothing, and keying by it makes anchoring idempotent. |
| Redis lock around a `pending` nonce | One worker per relayer key | Any number of workers can anchor. A transaction lost after sending is safe to resend. |
| stdlib `ThreadingHTTPServer` in the enclave, with a connection cap and read deadlines | uvicorn | Less code inside the measurement for two endpoints. |
| Extraction in worker processes that are killed at the deadline | A timeout around a thread | A thread cannot be stopped, so its work keeps the CPU after the request gives up. |
| PCR0 over build inputs | A hash of the Docker image | Image digests change between identical builds. The inputs reproduce from any checkout. |
| anvil | Ganache | Ganache is retired, and anvil ships with the Foundry toolchain the contract tests use. |

## Roadmap: milestone 2

- Build a real Nitro EIF with `nitro-cli build-enclave`, record its PCRs with each release, and
  have clients pin those.
- Release the document key from KMS under a key policy on `kms:RecipientAttestation:PCR0`, so a
  restarted enclave gets the same key and `enclave_key_rotated` goes away.
- Replace the internal Docker network with a vsock proxy between parent and enclave.
- Read attestations from `/dev/nsm` and pin the AWS Nitro root instead of the mock's.
- Batch receipt hashes into a Merkle transparency log and anchor one root per batch, with
  inclusion proofs for clients.
