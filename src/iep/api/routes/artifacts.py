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


@router.post("/dossiers/{dossier_id}/reports", response_model=DossierReport, status_code=201)
def generate_report(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
) -> DossierReport:
    dossiers.get(session, dossier_id)
    rendered = render.render_html(session, dossier_id)
    row = render.persist(session, dossier_id, rendered, report_root=settings.report_root)
    session.flush()
    return DossierReport.model_validate(row)


@router.get("/dossiers/{dossier_id}/reports", response_model=list[DossierReport])
def list_reports(
    dossier_id: uuid.UUID, session: Session = Depends(db_session)
) -> list[DossierReport]:
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


@router.get("/dossiers/{dossier_id}/export.json", response_class=Response)
def export_json(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    dossiers.get(session, dossier_id)
    return Response(
        content=render.export_json(session, dossier_id),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{dossier_id}.json"'},
    )


@router.get("/dossiers/{dossier_id}/export.csv", response_class=Response)
def export_csv(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    dossiers.get(session, dossier_id)
    return Response(
        content=render.export_csv(session, dossier_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{dossier_id}.csv"'},
    )


@router.get("/dossiers/{dossier_id}/audit", response_model=list[AuditEvent])
def dossier_audit(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[AuditEvent]:
    dossiers.get(session, dossier_id)
    return [
        AuditEvent.model_validate(row) for row in audit.history(session, dossier_id, limit=limit)
    ]


@router.get("/dossiers/{dossier_id}/evidence", summary="Lexical evidence lookup")
def find_evidence(
    dossier_id: uuid.UUID,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=5, ge=1, le=50),
    session: Session = Depends(db_session),
) -> dict[str, object]:
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
