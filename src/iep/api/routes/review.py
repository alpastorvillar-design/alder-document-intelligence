"""Extractions, findings and the human decisions taken on them."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.deps import db_session, require_api_key
from iep.db.models import Extraction as ExtractionRow
from iep.db.models import Finding as FindingRow
from iep.db.models import ReviewDecision as DecisionRow
from iep.domain.contracts import (
    DossierDecision,
    Extraction,
    FindingResolution,
    ReviewConfirmation,
    ReviewCorrection,
    ReviewDecision,
    ValidationFinding,
)
from iep.domain.enums import FieldStatus, FindingStatus, Severity
from iep.dossiers import service as dossiers
from iep.review import service as review

router = APIRouter(tags=["review"], dependencies=[Depends(require_api_key)])


@router.get("/dossiers/{dossier_id}/extractions", response_model=list[Extraction])
def list_extractions(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    field_status: FieldStatus | None = None,
    field_path: str | None = Query(default=None, max_length=200),
) -> list[Extraction]:
    dossiers.get(session, dossier_id)
    stmt = (
        select(ExtractionRow)
        .where(ExtractionRow.dossier_id == dossier_id)
        .order_by(ExtractionRow.field_path.asc())
    )
    if field_status is not None:
        stmt = stmt.where(ExtractionRow.status == field_status)
    if field_path:
        stmt = stmt.where(ExtractionRow.field_path.startswith(field_path))
    return [Extraction.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get("/dossiers/{dossier_id}/findings", response_model=list[ValidationFinding])
def list_findings(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    finding_status: FindingStatus | None = None,
    severity: Severity | None = None,
) -> list[ValidationFinding]:
    dossiers.get(session, dossier_id)
    stmt = (
        select(FindingRow)
        .where(FindingRow.dossier_id == dossier_id)
        .order_by(FindingRow.rule_id.asc())
    )
    if finding_status is not None:
        stmt = stmt.where(FindingRow.status == finding_status)
    if severity is not None:
        stmt = stmt.where(FindingRow.severity == severity)
    return [ValidationFinding.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get("/dossiers/{dossier_id}/decisions", response_model=list[ReviewDecision])
def list_decisions(
    dossier_id: uuid.UUID, session: Session = Depends(db_session)
) -> list[ReviewDecision]:
    dossiers.get(session, dossier_id)
    stmt = (
        select(DecisionRow)
        .where(DecisionRow.dossier_id == dossier_id)
        .order_by(DecisionRow.created_at.asc())
    )
    return [ReviewDecision.model_validate(row) for row in session.execute(stmt).scalars()]


@router.post("/extractions/{extraction_id}/correct", response_model=Extraction)
def correct_extraction(
    extraction_id: uuid.UUID,
    payload: ReviewCorrection,
    session: Session = Depends(db_session),
) -> Extraction:
    row = review.correct_field(
        session,
        extraction_id,
        actor=payload.actor,
        reason=payload.reason,
        new_value=payload.new_value,
    )
    return Extraction.model_validate(row)


@router.post("/extractions/{extraction_id}/confirm", response_model=Extraction)
def confirm_extraction(
    extraction_id: uuid.UUID,
    payload: ReviewConfirmation,
    session: Session = Depends(db_session),
) -> Extraction:
    row = review.confirm_field(session, extraction_id, actor=payload.actor, reason=payload.reason)
    return Extraction.model_validate(row)


@router.post("/findings/{finding_id}/resolve", response_model=ValidationFinding)
def resolve_finding(
    finding_id: uuid.UUID,
    payload: FindingResolution,
    session: Session = Depends(db_session),
) -> ValidationFinding:
    row = review.resolve_finding(
        session,
        finding_id,
        actor=payload.actor,
        reason=payload.reason,
        accept=payload.accept,
    )
    return ValidationFinding.model_validate(row)


@router.post("/dossiers/{dossier_id}/approve", status_code=204)
def approve_dossier(
    dossier_id: uuid.UUID,
    payload: DossierDecision,
    session: Session = Depends(db_session),
) -> None:
    review.approve(session, dossier_id, actor=payload.actor, reason=payload.reason)


@router.post("/dossiers/{dossier_id}/reject", status_code=204)
def reject_dossier(
    dossier_id: uuid.UUID,
    payload: DossierDecision,
    session: Session = Depends(db_session),
) -> None:
    review.reject(session, dossier_id, actor=payload.actor, reason=payload.reason)
