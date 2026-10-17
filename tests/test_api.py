import base64
import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from enclave.service import EnclaveService
from parent import auth
from parent.models import ApiKey, Document
from protocol import cose, envelope
from protocol.attestation import AttestationDocument
from protocol.envelope import Binding
from tests.conftest import Stack


def sealed_body(enclave: EnclaveService, tenant_id: str) -> dict[str, str]:
    document_id = uuid4()
    public_key = envelope.load_public_key(_attested_public_key(enclave))
    sealed = envelope.seal(
        public_key, b"irrelevant", Binding(tenant_id, document_id).aad("request")
    )
    return {"document_id": str(document_id), "envelope": base64.b64encode(sealed.encode()).decode()}


def _attested_public_key(enclave: EnclaveService) -> bytes:
    document = AttestationDocument.decode(cose.decode(enclave.attestation(os.urandom(32))).payload)
    assert document.public_key is not None
    return document.public_key


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    "headers",
    [{}, bearer("ldk_0000000000000000_wrong"), bearer("not-a-key"), {"X-Tenant-ID": "acme"}],
    ids=["none", "unknown key", "malformed", "old tenant header"],
)
def test_requests_without_a_valid_api_key_are_rejected(
    stack: Stack, headers: dict[str, str]
) -> None:
    stack.tenant("acme")
    for method, path in [("GET", "/documents"), ("GET", "/tenant"), ("POST", "/documents")]:
        response = stack.http.request(method, path, headers=headers, json={})
        assert response.status_code == 401, (method, path)


def test_a_revoked_key_stops_working(stack: Stack) -> None:
    token = stack.tenant("acme")
    assert stack.http.get("/tenant", headers=bearer(token)).json() == {"tenant_id": "acme"}
    with stack.sessions() as session:
        assert auth.revoke(session, token.split("_")[1])
    assert stack.http.get("/tenant", headers=bearer(token)).status_code == 401


def test_api_keys_are_stored_only_as_hashes(stack: Stack) -> None:
    token = stack.tenant("acme")
    secret = token.split("_", 2)[2]
    assert secret.encode() not in stack.database.read_bytes()
    with stack.sessions() as session:
        stored = session.scalars(select(ApiKey)).one()
    assert len(stored.secret_hash) == 32


def test_tenant_a_cannot_read_tenant_b_documents(stack: Stack, enclave: EnclaveService) -> None:
    acme, globex = stack.tenant("acme"), stack.tenant("globex")
    body = sealed_body(enclave, "acme")
    assert stack.http.post("/documents", json=body, headers=bearer(acme)).status_code == 202
    document_id = body["document_id"]

    assert stack.http.get(f"/documents/{document_id}", headers=bearer(acme)).status_code == 200
    assert stack.http.get(f"/documents/{document_id}", headers=bearer(globex)).status_code == 404
    assert stack.http.get("/documents", headers=bearer(globex)).json() == {"documents": []}


def test_document_ids_do_not_leak_across_tenants(stack: Stack, enclave: EnclaveService) -> None:
    acme, globex = stack.tenant("acme"), stack.tenant("globex")
    body = sealed_body(enclave, "acme")
    assert stack.http.post("/documents", json=body, headers=bearer(acme)).status_code == 202
    assert stack.http.post("/documents", json=body, headers=bearer(acme)).status_code == 409
    assert stack.http.post("/documents", json=body, headers=bearer(globex)).status_code == 202
    with stack.sessions() as session:
        assert len(session.scalars(select(Document)).all()) == 2


def test_the_parent_offers_no_way_to_decrypt(stack: Stack) -> None:
    token = stack.tenant("acme")
    response = stack.http.post("/decrypt", json={"encrypted_data": "AAAA"}, headers=bearer(token))
    assert response.status_code == 404
    paths = {getattr(route, "path", "") for route in stack.http.app.routes}  # type: ignore[attr-defined]
    assert not any("decrypt" in path for path in paths)


@pytest.mark.parametrize(
    "envelope_b64",
    [base64.b64encode(b"plaintext is not welcome").decode(), "%%% not base64 %%%"],
    ids=["unsealed bytes", "not base64"],
)
def test_only_sealed_envelopes_are_accepted(stack: Stack, envelope_b64: str) -> None:
    token = stack.tenant("acme")
    body = {"document_id": str(uuid4()), "envelope": envelope_b64}
    assert stack.http.post("/documents", json=body, headers=bearer(token)).status_code == 422
    assert stack.enqueued == []


def test_oversized_requests_are_refused(stack: Stack) -> None:
    token = stack.tenant("acme")
    body = {"document_id": str(uuid4()), "envelope": "A" * 200_000}
    assert stack.http.post("/documents", json=body, headers=bearer(token)).status_code == 413


def test_the_queue_only_carries_ids(stack: Stack, enclave: EnclaveService) -> None:
    token = stack.tenant("acme")
    stack.process_inline = False
    body = sealed_body(enclave, "acme")
    stack.http.post("/documents", json=body, headers=bearer(token))
    assert [(tenant, str(doc)) for tenant, doc in stack.enqueued] == [("acme", body["document_id"])]
