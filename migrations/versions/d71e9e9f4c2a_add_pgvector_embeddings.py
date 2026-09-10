"""Add pgvector-backed embeddings to evidence chunks

Revision ID: d71e9e9f4c2a
Revises: 529365ef6190
Create Date: 2026-09-10 10:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from iep.config import EMBEDDING_DIMENSIONS

revision: str = "d71e9e9f4c2a"
down_revision: str | None = "529365ef6190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Extensions are database-wide, while the migration test deliberately
    # builds the app in a non-public search_path. Qualifying the extension
    # schema makes the vector type resolvable in both cases.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.execute(
        f"ALTER TABLE document_chunks ADD COLUMN embedding public.vector({EMBEDDING_DIMENSIONS})"
    )
    op.add_column(
        "document_chunks", sa.Column("embedding_provider", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "document_chunks", sa.Column("embedding_model", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "document_chunks",
        sa.Column("embedding_config_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "document_chunks",
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_chunks", "embedded_at")
    op.drop_column("document_chunks", "embedding_config_hash")
    op.drop_column("document_chunks", "embedding_model")
    op.drop_column("document_chunks", "embedding_provider")
    op.drop_column("document_chunks", "embedding")
    # Do not drop the extension: another schema in the same database may use it.
