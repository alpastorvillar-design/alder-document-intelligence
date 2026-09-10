"""Lexical, vector and hybrid evidence lookup within one dossier."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from iep.db.models import Document, DocumentChunk

SEARCH_CONFIG = "spanish"


@dataclass(frozen=True)
class EvidenceHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_name: str
    ordinal: int
    text: str
    locator: dict[str, Any]
    rank: float
    lexical_rank: float | None = None
    vector_similarity: float | None = None


SearchMode = Literal["lexical", "vector", "hybrid"]


def search(
    session: Session, dossier_id: uuid.UUID, query: str, *, limit: int = 5
) -> list[EvidenceHit]:
    """Top-k segments for `query`, reproducibly ordered.

    Ties are broken by document id and ordinal rather than left to the planner,
    so the same query over the same corpus returns the same list every time -
    which is what makes the retrieval numbers in docs/measured-results.md
    meaningful.
    """
    cleaned = query.strip()
    if not cleaned:
        return []

    tsquery = func.plainto_tsquery(SEARCH_CONFIG, cleaned)
    rank = func.ts_rank(DocumentChunk.search_vector, tsquery).label("rank")

    stmt = (
        select(DocumentChunk, Document.original_filename, rank)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.dossier_id == dossier_id,
            DocumentChunk.search_vector.op("@@")(tsquery),
        )
        .order_by(rank.desc(), DocumentChunk.document_id.asc(), DocumentChunk.ordinal.asc())
        .limit(max(1, min(limit, 50)))
    )

    return [
        EvidenceHit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_name=filename,
            ordinal=chunk.ordinal,
            text=chunk.text,
            locator=chunk.locator,
            rank=float(score),
            lexical_rank=float(score),
        )
        for chunk, filename, score in session.execute(stmt).all()
    ]


def vector_search(
    session: Session,
    dossier_id: uuid.UUID,
    query_vector: tuple[float, ...],
    *,
    embedding_config_hash: str,
    limit: int = 5,
) -> list[EvidenceHit]:
    """Exact cosine search over embeddings created with the same configuration."""
    distance = cast(DocumentChunk.embedding.op("<=>")(list(query_vector)), Float)
    labelled_distance = distance.label("distance")
    stmt = (
        select(DocumentChunk, Document.original_filename, labelled_distance)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            DocumentChunk.dossier_id == dossier_id,
            DocumentChunk.embedding.is_not(None),
            DocumentChunk.embedding_config_hash == embedding_config_hash,
        )
        .order_by(distance.asc(), DocumentChunk.document_id.asc(), DocumentChunk.ordinal.asc())
        .limit(max(1, min(limit, 50)))
    )
    return [
        EvidenceHit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_name=filename,
            ordinal=chunk.ordinal,
            text=chunk.text,
            locator=chunk.locator,
            rank=1.0 - float(score),
            vector_similarity=1.0 - float(score),
        )
        for chunk, filename, score in session.execute(stmt).all()
    ]


def hybrid_search(
    session: Session,
    dossier_id: uuid.UUID,
    query: str,
    query_vector: tuple[float, ...],
    *,
    embedding_config_hash: str,
    limit: int = 5,
) -> list[EvidenceHit]:
    """Fuse lexical and vector rankings with reciprocal-rank fusion.

    RRF combines positions rather than incomparable raw scores. A stable chunk
    id tie-break keeps replayed queries deterministic.
    """
    candidate_limit = min(50, max(limit * 4, 20))
    lexical = search(session, dossier_id, query, limit=candidate_limit)
    vector = vector_search(
        session,
        dossier_id,
        query_vector,
        embedding_config_hash=embedding_config_hash,
        limit=candidate_limit,
    )
    by_id = {hit.chunk_id: hit for hit in [*lexical, *vector]}
    scores: dict[uuid.UUID, float] = dict.fromkeys(by_id, 0.0)
    lexical_scores = {hit.chunk_id: hit.rank for hit in lexical}
    vector_scores = {hit.chunk_id: hit.rank for hit in vector}
    rrf_k = 60
    for position, hit in enumerate(lexical, start=1):
        scores[hit.chunk_id] += 1.0 / (rrf_k + position)
    for position, hit in enumerate(vector, start=1):
        scores[hit.chunk_id] += 1.0 / (rrf_k + position)

    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], str(chunk_id)))
    return [
        replace(
            by_id[chunk_id],
            rank=scores[chunk_id],
            lexical_rank=lexical_scores.get(chunk_id),
            vector_similarity=vector_scores.get(chunk_id),
        )
        for chunk_id in ordered[: max(1, min(limit, 50))]
    ]
