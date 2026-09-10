"""Explicit re-indexing when an embedding provider or model changes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.config import Settings
from iep.db.models import DocumentChunk
from iep.retrieval.embeddings import (
    EmbeddingConfigurationError,
    build_embedding_provider,
    embed_in_batches,
)


@dataclass(frozen=True)
class ReindexResult:
    chunks_seen: int
    chunks_updated: int
    provider: str
    model: str
    config_hash: str


def reindex_dossier(session: Session, dossier_id: uuid.UUID, settings: Settings) -> ReindexResult:
    provider = build_embedding_provider(settings)
    if provider is None:
        raise EmbeddingConfigurationError("Cannot re-index while embeddings are disabled.")
    config_hash = provider.config_hash()
    rows = list(
        session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.dossier_id == dossier_id)
            .order_by(DocumentChunk.document_id.asc(), DocumentChunk.ordinal.asc())
        ).scalars()
    )
    pending = [
        row for row in rows if row.embedding is None or row.embedding_config_hash != config_hash
    ]
    vectors = embed_in_batches(
        provider,
        [row.text for row in pending],
        batch_size=settings.embedding_batch_size,
    )
    now = datetime.now(UTC)
    for row, vector in zip(pending, vectors, strict=True):
        row.embedding = list(vector)
        row.embedding_provider = provider.name
        row.embedding_model = provider.model
        row.embedding_config_hash = config_hash
        row.embedded_at = now
    return ReindexResult(
        chunks_seen=len(rows),
        chunks_updated=len(pending),
        provider=provider.name,
        model=provider.model,
        config_hash=config_hash,
    )
