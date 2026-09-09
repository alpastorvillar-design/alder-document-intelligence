"""Add review revisions and scope job idempotency to a dossier.

Revision ID: 3f6a2cc1d819
Revises: 8e23a202fc39
Create Date: 2026-09-09 20:00:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3f6a2cc1d819"
down_revision: str | None = "8e23a202fc39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extractions",
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
    )
    op.drop_constraint("uq_job_idempotency_key", "processing_jobs", type_="unique")
    op.create_unique_constraint(
        "uq_job_dossier_idempotency_key",
        "processing_jobs",
        ["dossier_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_job_dossier_idempotency_key", "processing_jobs", type_="unique")
    op.create_unique_constraint("uq_job_idempotency_key", "processing_jobs", ["idempotency_key"])
    op.drop_column("extractions", "revision")
