"""Initial schema - documents and extracted_fields tables

Revision ID: 001
Revises:
Create Date: 2026-03-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("encrypted_content", sa.LargeBinary, nullable=False),
        sa.Column("encrypted_result", sa.LargeBinary, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')",
            name="ck_documents_status",
        ),
    )

    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_status", "documents", ["status"])
    op.create_index("ix_documents_tenant_id_status", "documents", ["tenant_id", "status"])

    op.create_table(
        "extracted_fields",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_index("ix_extracted_fields_document_id", "extracted_fields", ["document_id"])
    op.create_index("ix_extracted_fields_tenant_id", "extracted_fields", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("extracted_fields")
    op.drop_table("documents")
