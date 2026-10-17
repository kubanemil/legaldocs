"""Celery tasks. Arguments are ids only; ciphertext never passes through the broker."""

import os
from functools import cache
from uuid import UUID

import redis
from celery import Celery, Task
from celery.signals import worker_init
from redis.lock import Lock
from sqlalchemy.orm import Session, sessionmaker
from web3 import Web3
from web3.exceptions import Web3Exception

from parent import pipeline
from parent.chain import ChainError, Relayer
from parent.config import Settings
from parent.db import session_factory
from parent.enclave_link import AttestedTlsLink, EnclaveUnavailable

app = Celery("legaldocs", broker=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)


@cache
def _settings() -> Settings:
    return Settings.from_env()


@cache
def _sessions() -> sessionmaker[Session]:
    return session_factory(_settings().database_url)


@cache
def _link() -> AttestedTlsLink:
    settings = _settings()
    return AttestedTlsLink(
        settings.enclave_host,
        settings.enclave_port,
        client_cert=settings.parent_cert,
        client_key=settings.parent_key,
        trust_anchor=settings.nsm_trust_anchor,
        expected_pcr0=settings.expected_pcr0,
    )


@cache
def _relayer() -> Relayer:
    settings = _settings()
    client = redis.Redis.from_url(settings.redis_url)

    def nonce_lock() -> Lock:
        lock: Lock = client.lock("legaldocs:relayer-nonce", timeout=60, blocking_timeout=30)
        return lock

    w3 = Web3(Web3.HTTPProvider(settings.eth_rpc_url))
    return Relayer(w3, settings.registry_address, settings.relayer_key, nonce_lock)


# Celery logs and swallows exceptions from signal handlers; SystemExit is not swallowed.
@worker_init.connect
def check_configuration(**_: object) -> None:
    try:
        _settings()
        _relayer()
    except (KeyError, ValueError, ChainError, Web3Exception, OSError) as exc:
        raise SystemExit(f"worker is misconfigured: {exc!r}") from exc


@app.task(bind=True, max_retries=5)
def process(self: Task, tenant_id: str, document_id: str) -> None:
    with _sessions()() as session:
        try:
            completed = pipeline.process_document(session, _link(), tenant_id, UUID(document_id))
        except EnclaveUnavailable as exc:
            if self.request.retries >= self.max_retries:
                pipeline.give_up(session, tenant_id, UUID(document_id))
                return
            raise self.retry(exc=exc, countdown=2**self.request.retries) from exc
    if completed:
        anchor.delay(tenant_id, document_id)


@app.task(bind=True, max_retries=8)
def anchor(self: Task, tenant_id: str, document_id: str) -> None:
    with _sessions()() as session:
        try:
            pipeline.anchor_document(session, _relayer(), tenant_id, UUID(document_id))
        except (ChainError, Web3Exception, OSError) as exc:
            raise self.retry(exc=exc, countdown=min(2**self.request.retries, 60)) from exc


def enqueue(tenant_id: str, document_id: UUID) -> None:
    process.delay(tenant_id, str(document_id))
