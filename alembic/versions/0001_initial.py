"""Tenants, hashed API keys, and documents that hold only ciphertext and receipts.

Replaces the pre-attestation schema. Rows from it were sealed to enclave keys that lived
only in memory, so there is nothing to carry over.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("envelope", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("sealed_result", sa.LargeBinary(), nullable=True),
        sa.Column("receipt", sa.LargeBinary(), nullable=True),
        sa.Column("receipt_hash", sa.LargeBinary(32), nullable=True),
        sa.Column("anchor_tx", sa.String(66), nullable=True),
        sa.Column("anchored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')", name="ck_documents_status"
        ),
    )
    op.create_index("ix_documents_tenant_status", "documents", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("api_keys")
    op.drop_table("tenants")
