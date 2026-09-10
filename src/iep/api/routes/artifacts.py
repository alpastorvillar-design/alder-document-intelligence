"""Reports, exports, audit history and evidence lookup."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.deps import db_session, require_api_key, settings_dep
from iep.api.errors import NotFoundError, ServiceUnavailableError
from iep.audit import service as audit
from iep.config import Settings
from iep.db.models import Report as ReportRow
from iep.domain.contracts import AuditEvent, DossierReport, RagAnswer, RagCitation, RagQuestion
from iep.dossiers import service as dossiers
from iep.reporting import render
from iep.retrieval import search as retrieval
from iep.retrieval.embeddings import EmbeddingProviderError, build_embedding_provider
from iep.retrieval.rag import RagProviderError, build_rag_generator

router = APIRouter(tags=["artifacts"], dependencies=[Depends(require_api_key)])


@router.post(
    "/dossiers/{dossier_id}/reports",
    response_model=DossierReport,
    status_code=201,
    summary="Render the report for a human",
)
def generate_report(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> DossierReport:
    """Renders a self-contained HTML report and records its content hash.

    The report is a snapshot: it stores the dossier state and the counts it
    was generated from, so an old report still says what was true when it
    was produced. Read it back at `reports/latest.html`.
    """
    dossiers.get(session, dossier_id)
    rendered = render.render_html(session, dossier_id)
    row = render.persist(session, dossier_id, rendered, report_root=settings.report_root)
    session.flush()
    return DossierReport.model_validate(row)


@router.get(
    "/dossiers/{dossier_id}/reports",
    response_model=list[DossierReport],
    summary="Reports generated so far",
)
def list_reports(
    dossier_id: uuid.UUID, session: Session = Depends(db_session)
) -> list[DossierReport]:
    """Newest first, each with its content hash and the state it was rendered under."""
    dossiers.get(session, dossier_id)
    stmt = (
        select(ReportRow)
        .where(ReportRow.dossier_id == dossier_id)
        .order_by(ReportRow.generated_at.desc())
    )
    return [DossierReport.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get(
    "/dossiers/{dossier_id}/reports/latest.html",
    response_class=Response,
    summary="The most recent rendered report",
)
def latest_report(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """The report itself, as HTML. Open it in a browser.

    `404` until one has been generated - see `POST .../reports`.
    """
    dossiers.get(session, dossier_id)
    row = session.execute(
        select(ReportRow)
        .where(ReportRow.dossier_id == dossier_id)
        .order_by(ReportRow.generated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("No report has been generated for this dossier yet.")
    path = settings.report_root / row.storage_key
    if not path.exists():
        raise NotFoundError("The stored report file is missing.", {"report_id": str(row.id)})
    return Response(content=path.read_bytes(), media_type="text/html; charset=utf-8")


@router.get(
    "/dossiers/{dossier_id}/export.json",
    response_class=Response,
    summary="Everything, as JSON, for another system",
)
def export_json(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    """The dossier, its documents, every extraction with its locator, and every finding.

    This is the machine-readable equivalent of the report: a downstream
    system gets the evidence, not just the totals.
    """
    dossiers.get(session, dossier_id)
    return Response(
        content=render.export_json(session, dossier_id),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{dossier_id}.json"'},
    )


@router.get(
    "/dossiers/{dossier_id}/export.csv",
    response_class=Response,
    summary="Everything, as CSV, for a spreadsheet",
)
def export_csv(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    """One row per extraction, with the locator rendered as readable text.

    Cells that begin with `=`, `+`, `-` or `@` are neutralised before they
    are written: a value read out of an untrusted document must not become
    a formula when somebody opens the file in Excel.
    """
    dossiers.get(session, dossier_id)
    return Response(
        content=render.export_csv(session, dossier_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{dossier_id}.csv"'},
    )


@router.get(
    "/dossiers/{dossier_id}/audit",
    response_model=list[AuditEvent],
    summary="Everything that ever happened to this dossier",
)
def dossier_audit(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[AuditEvent]:
    """Append-only: ingestion, rejections, jobs, transitions, human decisions.

    Each event is written inside the transaction of the change it describes,
    so a rollback cannot leave a record claiming something happened. Each
    carries the `correlation_id` of the request that caused it.
    """
    dossiers.get(session, dossier_id)
    return [
        AuditEvent.model_validate(row) for row in audit.history(session, dossier_id, limit=limit)
    ]


@router.get(
    "/dossiers/{dossier_id}/evidence",
    summary="Find a phrase inside this dossier's documents",
)
def find_evidence(
    dossier_id: uuid.UUID,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=5, ge=1, le=50),
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
    mode: retrieval.SearchMode = Query(default="lexical"),
) -> dict[str, object]:
    """Search only this dossier, returning the original evidence locators.

    `lexical` uses Spanish PostgreSQL full-text search. `vector` uses exact
    pgvector cosine distance. `hybrid` combines both rankings with RRF. This
    endpoint is retrieval, not generation; the separate questions endpoint is
    the optional RAG boundary.
    """
    dossiers.get(session, dossier_id)
    provider = None
    if mode == "lexical":
        hits = retrieval.search(session, dossier_id, q, limit=limit)
        method = "postgresql Spanish full-text search"
    else:
        try:
            provider = build_embedding_provider(settings)
            if provider is None:
                raise ServiceUnavailableError(
                    "Vector retrieval is disabled. Configure an embedding provider first."
                )
            query_vector = provider.embed([q]).vectors[0]
            if mode == "vector":
                hits = retrieval.vector_search(
                    session,
                    dossier_id,
                    query_vector,
                    embedding_config_hash=provider.config_hash(),
                    limit=limit,
                )
                method = "exact pgvector cosine search"
            else:
                hits = retrieval.hybrid_search(
                    session,
                    dossier_id,
                    q,
                    query_vector,
                    embedding_config_hash=provider.config_hash(),
                    limit=limit,
                )
                method = "reciprocal-rank fusion of lexical and pgvector search"
        except EmbeddingProviderError as exc:
            raise ServiceUnavailableError("Vector retrieval is unavailable.") from exc
    return {
        "query": q,
        "mode": mode,
        "method": method,
        "embedding": (
            {
                "provider": provider.name,
                "model": provider.model,
                "learned_model": provider.name != "hashing",
            }
            if provider is not None
            else None
        ),
        "results": [
            {
                "document": hit.document_name,
                "document_id": str(hit.document_id),
                "ordinal": hit.ordinal,
                "rank": round(hit.rank, 6),
                "lexical_rank": (
                    round(hit.lexical_rank, 6) if hit.lexical_rank is not None else None
                ),
                "vector_similarity": (
                    round(hit.vector_similarity, 6) if hit.vector_similarity is not None else None
                ),
                "text": hit.text,
                "locator": hit.locator,
                "where": render.describe_locator(hit.locator),
            }
            for hit in hits
        ],
    }


@router.post(
    "/dossiers/{dossier_id}/questions",
    response_model=RagAnswer,
    summary="Draft a cited answer from this dossier's evidence",
)
def answer_evidence_question(
    dossier_id: uuid.UUID,
    request: RagQuestion,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> RagAnswer:
    """Optional RAG boundary; disabled by default and never changes dossier state.

    Only the top-k chunks leave the process. The generator receives no tools,
    every returned citation is checked against those chunks, and document text
    is treated as untrusted data. A model answer is a draft, never a decision.
    """
    dossiers.get(session, dossier_id)
    try:
        if request.retrieval_mode == "lexical":
            hits = retrieval.search(session, dossier_id, request.question, limit=request.top_k)
        else:
            embedding_provider = build_embedding_provider(settings)
            if embedding_provider is None:
                raise ServiceUnavailableError(
                    "Vector retrieval is disabled. Configure an embedding provider first."
                )
            query_vector = embedding_provider.embed([request.question]).vectors[0]
            if request.retrieval_mode == "vector":
                hits = retrieval.vector_search(
                    session,
                    dossier_id,
                    query_vector,
                    embedding_config_hash=embedding_provider.config_hash(),
                    limit=request.top_k,
                )
            else:
                hits = retrieval.hybrid_search(
                    session,
                    dossier_id,
                    request.question,
                    query_vector,
                    embedding_config_hash=embedding_provider.config_hash(),
                    limit=request.top_k,
                )
        if not hits:
            raise ServiceUnavailableError(
                "No indexed evidence matches this retrieval configuration. Reprocess the dossier."
            )
        generation = build_rag_generator(settings).generate(request.question, hits)
    except (EmbeddingProviderError, RagProviderError) as exc:
        raise ServiceUnavailableError(
            "The optional grounded-answer provider is unavailable."
        ) from exc

    by_evidence_id = {f"E{position}": hit for position, hit in enumerate(hits, start=1)}
    citations = []
    for evidence_id in generation.citation_ids:
        hit = by_evidence_id[evidence_id]
        citations.append(
            RagCitation(
                evidence_id=evidence_id,
                document=hit.document_name,
                document_id=hit.document_id,
                ordinal=hit.ordinal,
                text=hit.text,
                locator=hit.locator,
                where=render.describe_locator(hit.locator),
            )
        )
    return RagAnswer(
        question=request.question,
        answer=generation.answer,
        sufficient_evidence=generation.sufficient_evidence,
        citations=citations,
        retrieval_mode=request.retrieval_mode,
        generation_provider=generation.provider,
        generation_model=generation.model,
        input_tokens=generation.input_tokens,
        output_tokens=generation.output_tokens,
        prompt_version=generation.prompt_version,
        prompt_sha256=generation.prompt_sha256,
    )
