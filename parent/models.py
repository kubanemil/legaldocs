from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Status(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Document(Base):
    """Everything here is ciphertext, a signature or a hash."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')", name="ck_documents_status"
        ),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    envelope: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[Status] = mapped_column(String(16), default=Status.PENDING)
    error_code: Mapped[str | None] = mapped_column(String(32))
    sealed_result: Mapped[bytes | None] = mapped_column(LargeBinary)
    receipt: Mapped[bytes | None] = mapped_column(LargeBinary)
    receipt_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    anchor_tx: Mapped[str | None] = mapped_column(String(66))
    anchored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
