"""Add blockchain anchoring fields to documents

Revision ID: 002
Revises: 001
Create Date: 2026-04-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_hash", sa.String(66), nullable=True))
    op.add_column("documents", sa.Column("result_hash", sa.String(66), nullable=True))
    op.add_column("documents", sa.Column("tx_hash", sa.String(66), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "tx_hash")
    op.drop_column("documents", "result_hash")
    op.drop_column("documents", "content_hash")
