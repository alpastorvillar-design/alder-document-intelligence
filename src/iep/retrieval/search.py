"""Evidence lookup over a dossier's own documents.

This is lexical search, and it is called that on purpose. PostgreSQL full-text
search over segments that already carry locators answers the question a
reviewer actually asks - "where does this dossier mention the eligible period?"
- and returns a page and a character span, not a paragraph of prose.

It is deliberately not RAG. Nothing generates text from these results; there is
no augmented generation, so calling it RAG would be a claim the code does not
support. `docs/ai-safety.md` records when embeddings would earn their place
here and when they would not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
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
        )
        for chunk, filename, score in session.execute(stmt).all()
    ]
