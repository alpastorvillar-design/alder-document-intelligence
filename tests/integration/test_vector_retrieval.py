"""Real PostgreSQL/pgvector checks; SQLite would not prove these operators."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from iep.config import Settings
from iep.db.models import Document, DocumentChunk, Dossier
from iep.domain.enums import (
    DocumentKind,
    DocumentStatus,
    MediaKind,
    SourceKind,
)
from iep.retrieval.embeddings import HashingEmbeddingProvider
from iep.retrieval.indexing import reindex_dossier
from iep.retrieval.rag import RagGeneration
from iep.retrieval.search import hybrid_search, vector_search


def add_chunk(db: Session, dossier: Dossier, content: str, suffix: str) -> DocumentChunk:
    provider = HashingEmbeddingProvider(512)
    document = Document(
        id=uuid.uuid4(),
        dossier_id=dossier.id,
        original_filename=f"evidence-{suffix}.pdf",
        declared_media_type="application/pdf",
        media_kind=MediaKind.PDF,
        document_kind=DocumentKind.TECHNICAL_REPORT,
        status=DocumentStatus.EXTRACTED,
        source_kind=SourceKind.UPLOAD,
        source_detail=None,
        size_bytes=100,
        content_sha256=suffix.zfill(64),
        storage_key=f"sha256/{suffix}",
        page_count=1,
        rejection_reason=None,
        alternate_filenames=[],
    )
    vector = provider.embed([content]).vectors[0]
    chunk = DocumentChunk(
        id=uuid.uuid4(),
        dossier_id=dossier.id,
        document_id=document.id,
        ordinal=0,
        text=content,
        locator={"kind": "PDF_PAGE", "page": 1},
        embedding=list(vector),
        embedding_provider=provider.name,
        embedding_model=provider.model,
        embedding_config_hash=provider.config_hash(),
        embedded_at=datetime.now(UTC),
    )
    db.add_all([document, chunk])
    db.flush()
    return chunk


class TestPgvectorRetrieval:
    def test_the_migrated_database_has_the_vector_extension(self, db: Session) -> None:
        version = db.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        assert version == "0.8.6"

    def test_exact_vector_search_is_scoped_to_one_dossier(self, db: Session) -> None:
        from tests.conftest import new_dossier

        first = new_dossier(db, "INN-2025-701")
        second = new_dossier(db, "INN-2025-702")
        target = add_chunk(db, first, "El periodo elegible termina en diciembre de 2025", "1")
        add_chunk(db, first, "Personal investigador y horas trabajadas", "2")
        other_dossier_chunk = add_chunk(
            db, second, "El periodo elegible termina en diciembre de 2025", "3"
        )
        provider = HashingEmbeddingProvider(512)
        query = provider.embed(["periodo elegible diciembre 2025"]).vectors[0]

        hits = vector_search(
            db,
            first.id,
            query,
            embedding_config_hash=provider.config_hash(),
            limit=10,
        )

        assert hits[0].chunk_id == target.id
        assert len(hits) == 2
        assert other_dossier_chunk.document_id not in {item.document_id for item in hits}
        assert all(item.vector_similarity is not None for item in hits)

    def test_hybrid_ranking_is_deterministic(self, db: Session) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db, "INN-2025-703")
        add_chunk(db, dossier, "Gastos elegibles dentro del periodo", "4")
        add_chunk(db, dossier, "Facturas y proveedores", "5")
        provider = HashingEmbeddingProvider(512)
        query = "gastos elegibles"
        vector = provider.embed([query]).vectors[0]

        first = hybrid_search(
            db,
            dossier.id,
            query,
            vector,
            embedding_config_hash=provider.config_hash(),
            limit=5,
        )
        second = hybrid_search(
            db,
            dossier.id,
            query,
            vector,
            embedding_config_hash=provider.config_hash(),
            limit=5,
        )
        assert [item.chunk_id for item in first] == [item.chunk_id for item in second]
        assert first[0].lexical_rank is not None
        assert first[0].vector_similarity is not None

    def test_reindex_updates_only_stale_vectors(self, db: Session, settings: Settings) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db, "INN-2025-707")
        chunk = add_chunk(db, dossier, "Costes de personal investigador", "9")
        assert reindex_dossier(db, dossier.id, settings).chunks_updated == 0

        chunk.embedding_config_hash = "0" * 64
        db.flush()
        refreshed = reindex_dossier(db, dossier.id, settings)
        assert refreshed.chunks_updated == 1
        assert chunk.embedding_config_hash == refreshed.config_hash


class TestRetrievalApi:
    def test_vector_mode_exposes_the_baseline_honestly(
        self, db: Session, wired_settings: Settings
    ) -> None:
        from iep.api.app import create_app
        from tests.conftest import new_dossier

        dossier = new_dossier(db, "INN-2025-704")
        add_chunk(db, dossier, "Periodo de ejecución hasta diciembre", "6")
        db.commit()
        with TestClient(create_app()) as client:
            response = client.get(
                f"/dossiers/{dossier.id}/evidence",
                params={"q": "periodo ejecucion", "mode": "vector"},
            )
        assert response.status_code == 200
        assert response.json()["embedding"]["learned_model"] is False
        assert response.json()["results"]

    def test_rag_is_disabled_by_default(self, db: Session, wired_settings: Settings) -> None:
        from iep.api.app import create_app
        from tests.conftest import new_dossier

        dossier = new_dossier(db, "INN-2025-705")
        add_chunk(db, dossier, "Periodo de ejecución hasta diciembre", "7")
        db.commit()
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.post(
                f"/dossiers/{dossier.id}/questions",
                json={"question": "periodo termina", "retrieval_mode": "lexical"},
            )
        assert response.status_code == 503
        assert response.json()["error"] == "service_unavailable"
        assert "Traceback" not in response.text

    def test_rag_response_maps_only_retrieved_citations(
        self,
        db: Session,
        wired_settings: Settings,
        monkeypatch: Any,
    ) -> None:
        from iep.api.app import create_app
        from iep.api.routes import artifacts
        from tests.conftest import new_dossier

        dossier = new_dossier(db, "INN-2025-706")
        add_chunk(db, dossier, "El periodo termina el 31 de diciembre de 2025", "8")
        db.commit()

        class Generator:
            def generate(self, question: str, hits: object) -> RagGeneration:
                return RagGeneration(
                    answer="Termina el 31 de diciembre de 2025.",
                    citation_ids=("E1",),
                    sufficient_evidence=True,
                    provider="test-double",
                    model="not-a-model",
                    input_tokens=None,
                    output_tokens=None,
                    prompt_version="rag-grounded-answer/1.0.0",
                    prompt_sha256="a" * 64,
                )

        monkeypatch.setattr(artifacts, "build_rag_generator", lambda _: Generator())
        with TestClient(create_app()) as client:
            response = client.post(
                f"/dossiers/{dossier.id}/questions",
                json={"question": "periodo termina", "retrieval_mode": "lexical"},
            )

        assert response.status_code == 200
        assert response.json()["citations"][0]["evidence_id"] == "E1"
        assert response.json()["citations"][0]["where"] == "page 1"
        assert response.json()["generation_provider"] == "test-double"


class TestEmbeddingColumnAcceptsWhatProvidersReturn:
    """The providers return tuples, and the column has to take them.

    `pgvector.sqlalchemy.VECTOR` binds only a list or a numpy array. Every
    test and one of the two writers happened to call `list(...)` first, so a
    tuple reaching the column went unnoticed until a real run of the pipeline
    failed at the insert with `expected list or ndarray`. These write exactly
    what `embed_in_batches` hands back.
    """

    def test_a_tuple_from_the_provider_round_trips(self, db: Session) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        provider = HashingEmbeddingProvider(512)
        vector = provider.embed(["periodo de ejecucion del proyecto"]).vectors[0]
        assert isinstance(vector, tuple), "the guard only means something while this holds"

        document = Document(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            original_filename="memoria.pdf",
            declared_media_type="application/pdf",
            media_kind=MediaKind.PDF,
            document_kind=DocumentKind.TECHNICAL_REPORT,
            status=DocumentStatus.EXTRACTED,
            source_kind=SourceKind.UPLOAD,
            source_detail=None,
            size_bytes=100,
            content_sha256="a" * 64,
            storage_key="sha256/aa",
            page_count=1,
            rejection_reason=None,
            alternate_filenames=[],
        )
        chunk = DocumentChunk(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            document_id=document.id,
            ordinal=0,
            text="periodo de ejecucion del proyecto",
            locator={"kind": "PDF_PAGE", "page": 1},
            embedding=vector,
            embedding_provider=provider.name,
            embedding_model=provider.model,
            embedding_config_hash=provider.config_hash(),
            embedded_at=datetime.now(UTC),
        )
        db.add_all([document, chunk])
        db.flush()
        db.expire_all()

        stored = db.get(DocumentChunk, chunk.id)
        assert stored is not None
        assert stored.embedding is not None
        assert len(stored.embedding) == 512
        assert stored.embedding[0] == pytest.approx(vector[0], abs=1e-6)

    def test_clearing_a_vector_still_works(self, db: Session) -> None:
        """The pipeline nulls the vector when a chunk's text changes."""
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        chunk = add_chunk(db, dossier, "texto original", "b1")
        chunk.embedding = None
        chunk.embedding_provider = None
        db.flush()
        db.expire_all()

        stored = db.get(DocumentChunk, chunk.id)
        assert stored is not None
        assert stored.embedding is None
