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


@router.get(
    "/dossiers/{dossier_id}/extractions",
    response_model=list[Extraction],
    summary="Every field that was read, and where from",
)
def list_extractions(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    field_status: FieldStatus | None = None,
    field_path: str | None = Query(default=None, max_length=200),
) -> list[Extraction]:
    """The evidence layer: one row per value, each with a `locator`.

    A locator is a tagged union, so it says exactly what kind of place the
    value came from: a page and character span, an OCR word box with the
    engine's confidence, a sheet and cell, a JSON path in an API response, a
    CSS selector on a captured page, or the extraction ids a total was
    derived from.

    `field_path` matches by prefix - `invoice.` gives every invoice field.
    `status` is where a value sits in review, and `original_value_text` holds
    what the machine read when a person disagreed with it.
    """
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


@router.get(
    "/dossiers/{dossier_id}/findings",
    response_model=list[ValidationFinding],
    summary="What the rules concluded",
)
def list_findings(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    finding_status: FindingStatus | None = None,
    severity: Severity | None = None,
) -> list[ValidationFinding]:
    """Deterministic, versioned rules comparing every source against the others.

    `severity` is `BLOCKER`, `WARNING` or `INFO`; a `BLOCKER` that is still
    open or accepted prevents approval. Each finding names the rule, its
    version, the extractions and documents it points at, and a structured
    `detail` with the numbers it compared.
    """
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


@router.get(
    "/dossiers/{dossier_id}/decisions",
    response_model=list[ReviewDecision],
    summary="What each person decided, and why",
)
def list_decisions(
    dossier_id: uuid.UUID, session: Session = Depends(db_session)
) -> list[ReviewDecision]:
    """Append-only, oldest first: correct, confirm, accept, dismiss, approve, reject.

    Nothing here is ever updated or deleted, so the sequence is a record
    rather than a story.
    """
    dossiers.get(session, dossier_id)
    stmt = (
        select(DecisionRow)
        .where(DecisionRow.dossier_id == dossier_id)
        .order_by(DecisionRow.created_at.asc())
    )
    return [ReviewDecision.model_validate(row) for row in session.execute(stmt).scalars()]


@router.post(
    "/extractions/{extraction_id}/correct",
    response_model=Extraction,
    summary="This was misread; the value is X",
)
def correct_extraction(
    extraction_id: uuid.UUID,
    payload: ReviewCorrection,
    session: Session = Depends(db_session),
) -> Extraction:
    """Records a human value beside the machine's, never over it.

    `original_value_text` keeps what was read, and the actor, timestamp and
    reason are stored with the change. Pass `expected_revision` and a stale
    edit fails with `409` instead of silently overwriting somebody else's
    decision; the corrected value is then fenced from being overwritten by a
    re-run.
    """
    row = review.correct_field(
        session,
        extraction_id,
        actor=payload.actor,
        reason=payload.reason,
        new_value=payload.new_value,
        expected_revision=payload.expected_revision,
    )
    return Extraction.model_validate(row)


@router.post(
    "/extractions/{extraction_id}/confirm",
    response_model=Extraction,
    summary="I checked this against the document",
)
def confirm_extraction(
    extraction_id: uuid.UUID,
    payload: ReviewConfirmation,
    session: Session = Depends(db_session),
) -> Extraction:
    """Clears a field that was routed to review because its confidence was low.

    The value does not change; what changes is that a named person takes
    responsibility for it. Approval is blocked while any field still needs
    review.
    """
    row = review.confirm_field(
        session,
        extraction_id,
        actor=payload.actor,
        reason=payload.reason,
        expected_revision=payload.expected_revision,
    )
    return Extraction.model_validate(row)


@router.post(
    "/findings/{finding_id}/resolve",
    response_model=ValidationFinding,
    summary="Accept or dismiss a finding, with a reason",
)
def resolve_finding(
    finding_id: uuid.UUID,
    payload: FindingResolution,
    session: Session = Depends(db_session),
) -> ValidationFinding:
    """`accept: true` means the issue is real. It is not a waiver.

    An accepted blocker still prevents approval - the claim has to change,
    not the verdict. `accept: false` dismisses it as a false positive and
    requires a reason, which is recorded: a check that can be walked past
    silently is not a check.
    """
    row = review.resolve_finding(
        session,
        finding_id,
        actor=payload.actor,
        reason=payload.reason,
        accept=payload.accept,
    )
    return ValidationFinding.model_validate(row)


@router.post("/dossiers/{dossier_id}/approve", status_code=204, summary="Approve the dossier")
def approve_dossier(
    dossier_id: uuid.UUID,
    payload: DossierDecision,
    session: Session = Depends(db_session),
) -> None:
    """The only way a dossier is approved. No pipeline path reaches this state.

    Refused with `409` while any field still needs review or any blocker is
    open or accepted. An approved dossier is immutable: correcting one means
    creating a new dossier, so the audit trail stays a record of what was
    actually submitted.
    """
    review.approve(session, dossier_id, actor=payload.actor, reason=payload.reason)


@router.post("/dossiers/{dossier_id}/reject", status_code=204, summary="Reject the dossier")
def reject_dossier(
    dossier_id: uuid.UUID,
    payload: DossierDecision,
    session: Session = Depends(db_session),
) -> None:
    """Sends the claim back, with a reason, recorded against the named actor.

    Unlike approval this is not terminal: a rejected dossier can receive new
    documents and be processed again.
    """
    review.reject(session, dossier_id, actor=payload.actor, reason=payload.reason)
