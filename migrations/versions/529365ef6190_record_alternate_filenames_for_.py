"""Record the other filenames that carried byte-identical content

Revision ID: 529365ef6190
Revises: 3f6a2cc1d819
Create Date: 2026-09-09 19:54:34.490149+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "529365ef6190"
down_revision: str | None = "3f6a2cc1d819"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A server default is required: the column is NOT NULL and existing rows
    # have to get a value during the ALTER, not afterwards.
    op.add_column(
        "documents",
        sa.Column(
            "alternate_filenames",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "alternate_filenames")
