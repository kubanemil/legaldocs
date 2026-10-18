# legaldocs

The server in this repo stores and queues legal documents it cannot read. Only an attested
enclave opens them and pulls out the parties and dates.

Before sending anything, the client checks the enclave's attestation. It seals each document
to the enclave's key and gets the result back sealed to a key it made for that one request.
The enclave signs a receipt for every document, and the receipt's hash is timestamped on
Ethereum.

It all runs on one machine. A container stands in for the AWS Nitro enclave, a mock Nitro
Security Module (NSM) signs the attestations, and anvil is the chain. Attestations follow
Nitro's format, and nothing in the verifier knows it is talking to a mock.

You need Docker Compose, [uv](https://docs.astral.sh/uv/) and [Foundry](https://getfoundry.sh).

```
./do dev      # build and start the stack
./do demo     # two tenants send three documents through it
./do smoke    # fresh stack, end-to-end and privacy checks, teardown
./do test     # unit and contract tests, no Docker
```

## Threat model

The adversary is the parent: the API, the worker, Postgres and Redis. It can read and change
everything it stores, replay and reorder messages, and run any code in its own containers.
Against it, the local stack guarantees:

- The parent never holds a key that opens a document or a result. The client's reply key
  travels inside the sealed document, so the parent cannot swap in its own.
- The parent cannot pass off its own key as the enclave's. Only the NSM signs attestations,
  and the NSM has no network and shares its socket with the enclave alone.
- A recorded attestation cannot be replayed, and a different enclave build is refused. Each
  attestation carries the client's nonce, PCR0 and a timestamp, and the client checks all three.
- Every ciphertext is bound to its tenant, its document id and the protocol version, so one
  moved to another tenant or swapped between documents fails to open.
- A receipt commits to the input and the output under a salt only the client knows. Neither
  the receipt nor its hash on chain lets anyone confirm a guessed document.

This repo shows the protocol, not the isolation:

- The enclave is an ordinary container, so root on the host can read its memory, swap its
  image or run a different NSM.
- There is no hardware root of trust. The NSM makes its root key at boot, and `./do dev`
  copies its root certificate off the host for the client.
- PCR0 covers the build inputs, not the running bytes. It hashes the enclave's Dockerfile
  (digest-pinned base), hash-locked requirements and source, and the NSM measures its own copy
  of them, so a host that builds the two images from different trees defeats the check.
- Nothing stops the parent from dropping, delaying or refusing a request.

The enclave does see plaintext, since that is its job. Its logs leave it, so they carry
request lines and error types, never document content. Its keys never leave its memory, so a restart re-keys:
documents in flight fail with `enclave_key_rotated`, and the SDK's `process()` re-attests and
resubmits once.

`./do smoke` checks this on a live stack, and CI runs it on every pull request. It fails if a
test document shows up in a Postgres dump, a Redis dump or any container log, if the enclave
can reach the internet, Postgres, Redis, anvil or the NSM over the network, or if a host key
reaches the enclave. It also restarts the enclave mid-run to check the recovery above.

The code follows the same split: `client/`, `parent/`, `enclave/` and `nsm/`, with the shared
wire formats in `protocol/` and the registry contract in `contracts/`.

MIT licensed.
