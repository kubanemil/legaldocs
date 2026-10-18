"""Client SDK. Plaintext exists only here and inside an attested enclave.

The client checks the enclave's attestation before it seals anything, seals each document
to the attested key, and opens results with a key pair it made for that one request.
"""

import base64
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import httpx
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import x25519

from protocol import envelope, receipt
from protocol.attestation import EnclaveIdentity, Policy, verify_enclave
from protocol.envelope import Binding, Envelope
from protocol.messages import DocumentRequest

NONCE_BYTES: Final = 32
KEY_ROTATED: Final = "enclave_key_rotated"
ANCHORED_AT_SELECTOR: Final = "0x9591a610"


class ClientError(RuntimeError):
    pass


class ProcessingFailed(ClientError):
    def __init__(self, code: str) -> None:
        super().__init__(f"processing failed: {code}")
        self.code = code


@dataclass(frozen=True)
class Submission:
    binding: Binding
    identity: EnclaveIdentity
    reply_key: x25519.X25519PrivateKey
    salt: bytes
    input_hash: bytes

    @property
    def document_id(self) -> UUID:
        return self.binding.document_id


@dataclass(frozen=True)
class Result:
    document_id: UUID
    fields: dict[str, Any]
    receipt: receipt.Receipt
    receipt_hash: bytes
    anchor_tx: str | None
    anchored_at: str | None


class LegalDocsClient:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        trust_anchor: x509.Certificate,
        expected_pcr0: bytes,
        http: httpx.Client | None = None,
        max_attestation_age: timedelta = timedelta(minutes=5),
    ) -> None:
        self._http = http or httpx.Client(base_url=api_url, timeout=30.0)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._policy = Policy(trust_anchor, expected_pcr0, max_age=max_attestation_age)
        self._tenant_id: str | None = None

    @property
    def tenant_id(self) -> str:
        if self._tenant_id is None:
            self._tenant_id = str(self._get("/tenant")["tenant_id"])
        return self._tenant_id

    def attest(self) -> EnclaveIdentity:
        nonce = os.urandom(NONCE_BYTES)
        body = self._get("/attestation", params={"nonce": nonce.hex()})
        return verify_enclave(base64.b64decode(body["document"]), self._policy, nonce)

    def submit(self, content: bytes, identity: EnclaveIdentity | None = None) -> Submission:
        identity = identity or self.attest()
        binding = Binding(self.tenant_id, uuid4())
        reply_key = x25519.X25519PrivateKey.generate()
        salt = os.urandom(receipt.SALT_BYTES)
        request = DocumentRequest(
            content=content,
            reply_key=envelope.raw_public_key(reply_key.public_key()),
            salt=salt,
        )
        sealed = envelope.seal(identity.encryption_key, request.encode(), binding.aad("request"))
        self._post(
            "/documents",
            {
                "document_id": str(binding.document_id),
                "envelope": base64.b64encode(sealed.encode()).decode(),
            },
        )
        return Submission(binding, identity, reply_key, salt, receipt.commit(salt, content))

    def status(self, document_id: UUID) -> dict[str, Any]:
        return self._get(f"/documents/{document_id}")

    def list_documents(self) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = self._get("/documents")["documents"]
        return documents

    def wait(
        self, submission: Submission, *, timeout: float = 60.0, anchored: bool = False
    ) -> Result:
        deadline = time.monotonic() + timeout
        while True:
            document = self.status(submission.document_id)
            if document["status"] == "FAILED":
                raise ProcessingFailed(document["error_code"] or "unknown")
            if document["status"] == "COMPLETED" and (document["anchored_at"] or not anchored):
                return self.open(submission, document)
            if time.monotonic() > deadline:
                raise ClientError(f"document {submission.document_id} not ready after {timeout}s")
            time.sleep(0.25)

    def process(self, content: bytes, *, timeout: float = 60.0) -> Result:
        try:
            return self.wait(self.submit(content), timeout=timeout)
        except ProcessingFailed as failure:
            if failure.code != KEY_ROTATED:
                raise
            return self.wait(self.submit(content), timeout=timeout)

    def open(self, submission: Submission, document: dict[str, Any]) -> Result:
        sealed = Envelope.decode(base64.b64decode(document["sealed_result"]))
        plaintext = envelope.unseal(sealed, submission.reply_key, submission.binding.aad("result"))
        signed = base64.b64decode(document["receipt"])
        checked = receipt.verify(signed, submission.identity.signing_key)
        expected = (
            (checked.binding, submission.binding.digest()),
            (checked.input_hash, submission.input_hash),
            (checked.output_hash, receipt.commit(submission.salt, plaintext)),
            (checked.pcr0, submission.identity.pcr0),
        )
        if not all(hmac.compare_digest(got, want) for got, want in expected):
            raise ClientError("receipt does not match this document and result")
        receipt_hash = receipt.anchor_hash(signed)
        if document["receipt_hash"] != receipt_hash.hex():
            raise ClientError("parent reported a different receipt hash")
        return Result(
            document_id=submission.document_id,
            fields=json.loads(plaintext),
            receipt=checked,
            receipt_hash=receipt_hash,
            anchor_tx=document["anchor_tx"],
            anchored_at=document["anchored_at"],
        )

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._http.get(path, params=params, headers=self._headers)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def _post(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        response = self._http.post(path, json=payload, headers=self._headers)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


def anchored_at(
    rpc_url: str, registry: str, receipt_hash: bytes, http: httpx.Client | None = None
) -> int:
    """Read ReceiptRegistry.anchoredAt(receipt_hash) with a raw eth_call; 0 means not anchored."""
    call = {"to": registry, "data": ANCHORED_AT_SELECTOR + receipt_hash.hex()}
    request = {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [call, "latest"]}
    response = (http or httpx.Client(timeout=10.0)).post(rpc_url, json=request)
    response.raise_for_status()
    reply = response.json()
    if "error" in reply:
        raise ClientError(f"eth_call failed: {reply['error']}")
    return int(reply["result"], 16)
