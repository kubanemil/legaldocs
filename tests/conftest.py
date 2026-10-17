from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from client.sdk import LegalDocsClient
from enclave.service import EnclaveError, EnclaveKeys, EnclaveService
from nsm.device import MockNsm
from nsm.measure import measure
from parent import admin, pipeline
from parent.api import Services, create_app
from parent.enclave_link import EnclaveRejected
from parent.models import Base
from protocol.attestation import Policy
from protocol.messages import ProcessRequest, ProcessResponse

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def pcrs() -> dict[int, bytes]:
    return measure(ROOT)


@pytest.fixture(scope="session")
def nsm(pcrs: dict[int, bytes]) -> MockNsm:
    return MockNsm(pcrs)


@pytest.fixture
def policy(nsm: MockNsm, pcrs: dict[int, bytes]) -> Policy:
    return Policy(nsm.root_certificate, pcrs[0])


@pytest.fixture
def enclave(nsm: MockNsm) -> EnclaveService:
    return EnclaveService(nsm, EnclaveKeys.generate())


class InProcessLink:
    """The parent's view of the enclave, minus TLS, recording every byte that crosses."""

    def __init__(self, enclave: EnclaveService) -> None:
        self.enclave = enclave
        self.traffic: list[bytes] = []

    def attestation(self, nonce: bytes) -> bytes:
        document = self.enclave.attestation(nonce)
        self.traffic.append(document)
        return document

    def process(self, request: ProcessRequest) -> ProcessResponse:
        body = request.encode()
        self.traffic.append(body)
        try:
            response = self.enclave.process(body)
        except EnclaveError as exc:
            raise EnclaveRejected(exc.code, exc.status) from exc
        self.traffic.append(response)
        return ProcessResponse.decode(response)


class Stack:
    """API, worker pipeline and enclave in one process, on a SQLite file."""

    def __init__(self, database: Path, link: InProcessLink, max_envelope_bytes: int) -> None:
        engine = create_engine(
            f"sqlite+pysqlite:///{database}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        self.database = database
        self.link = link
        self.sessions: sessionmaker[Session] = sessionmaker(engine, expire_on_commit=False)
        self.enqueued: list[tuple[str, UUID]] = []
        self.process_inline = True
        app = create_app(Services(self.sessions, link, self._enqueue, max_envelope_bytes))
        self.http = TestClient(app)

    def _enqueue(self, tenant_id: str, document_id: UUID) -> None:
        self.enqueued.append((tenant_id, document_id))
        if self.process_inline:
            self.run(tenant_id, document_id)

    def run(self, tenant_id: str, document_id: UUID) -> None:
        with self.sessions() as session:
            pipeline.process_document(session, self.link, tenant_id, document_id)

    def tenant(self, tenant_id: str) -> str:
        with self.sessions() as session:
            return admin.create_tenant(session, tenant_id).token

    def client(self, token: str, nsm: MockNsm, pcr0: bytes) -> LegalDocsClient:
        return LegalDocsClient(
            "http://testserver",
            token,
            trust_anchor=nsm.root_certificate,
            expected_pcr0=pcr0,
            http=self.http,
        )


@pytest.fixture
def stack(tmp_path: Path, enclave: EnclaveService) -> Iterator[Stack]:
    stack = Stack(tmp_path / "parent.db", InProcessLink(enclave), max_envelope_bytes=64 * 1024)
    with stack.http:
        yield stack
