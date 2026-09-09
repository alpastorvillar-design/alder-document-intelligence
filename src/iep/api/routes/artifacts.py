"""Reports, exports, audit history and evidence lookup."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.deps import db_session, require_api_key, settings_dep
from iep.api.errors import NotFoundError
from iep.audit import service as audit
from iep.config import Settings
from iep.db.models import Report as ReportRow
from iep.domain.contracts import AuditEvent, DossierReport
from iep.dossiers import service as dossiers
from iep.reporting import render
from iep.retrieval import search as retrieval

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
) -> dict[str, object]:
    """PostgreSQL Spanish full-text search over the dossier's own segments.

    Every hit comes back with its locator and a readable `where`, so the
    answer is a place in a document rather than a paraphrase.

    This is **retrieval, not RAG**: nothing generates text from the result.
    Calling it retrieval-augmented generation would claim something the code
    does not do. What would justify embeddings, and what would make this RAG,
    is argued in `docs/adr/0004-lexical-retrieval-not-rag.md`.
    """
    dossiers.get(session, dossier_id)
    hits = retrieval.search(session, dossier_id, q, limit=limit)
    return {
        "query": q,
        "method": "postgresql full-text search over document segments",
        "results": [
            {
                "document": hit.document_name,
                "document_id": str(hit.document_id),
                "ordinal": hit.ordinal,
                "rank": round(hit.rank, 6),
                "text": hit.text,
                "locator": hit.locator,
                "where": render.describe_locator(hit.locator),
            }
            for hit in hits
        ],
    }
