"""Human review.

The original value is never destroyed. A correction sets the new value and
keeps the original in `original_value_text`, along with who changed it, when
and why, so a report can always show both and an auditor can see what the
machine read before a person disagreed with it.

Approval and rejection are the only ways a dossier leaves review, and both are
human actions with a recorded reason.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from iep.api.errors import ConflictError, NotFoundError
from iep.audit import service as audit
from iep.db.models import Extraction, Finding, ReviewDecision
from iep.domain.enums import (
    AuditAction,
    DecisionAction,
    DossierStatus,
    FieldStatus,
    FindingStatus,
    Severity,
)
from iep.dossiers import service as dossiers
from iep.extraction import parse
from iep.observability import metrics


def get_extraction(session: Session, extraction_id: uuid.UUID) -> Extraction:
    row = session.get(Extraction, extraction_id)
    if row is None:
        raise NotFoundError("No extraction with that id.", {"extraction_id": str(extraction_id)})
    return row


def get_finding(session: Session, finding_id: uuid.UUID) -> Finding:
    row = session.get(Finding, finding_id)
    if row is None:
        raise NotFoundError("No finding with that id.", {"finding_id": str(finding_id)})
    return row


def correct_field(
    session: Session,
    extraction_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    new_value: str,
    expected_revision: int | None = None,
) -> Extraction:
    extraction = get_extraction(session, extraction_id)
    dossier = dossiers.get(session, extraction.dossier_id)
    _require_open(dossier.status)

    previous = extraction.value_text
    original = extraction.original_value_text
    if original is None:
        original = previous
    _apply_field_update(
        session,
        extraction,
        expected_revision=expected_revision,
        values={
            "original_value_text": original,
            "value_text": new_value[:500],
            "value_number": parse.parse_amount(new_value),
            "value_date": parse.parse_date(new_value),
            "status": FieldStatus.CORRECTED,
            "corrected_by": actor,
            "corrected_at": datetime.now(UTC),
            "correction_reason": reason,
            "confidence": 1.0,
        },
    )

    _record(
        session,
        dossier_id=extraction.dossier_id,
        action=DecisionAction.CORRECT_FIELD,
        audit_action=AuditAction.FIELD_CORRECTED,
        actor=actor,
        reason=reason,
        extraction_id=extraction.id,
        new_value=new_value,
        payload={
            "field_path": extraction.field_path,
            "previous_value": previous,
            "new_value": new_value,
        },
    )
    metrics.increment("iep_review_actions_total", action="correct_field")
    return extraction


def confirm_field(
    session: Session,
    extraction_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    expected_revision: int | None = None,
) -> Extraction:
    extraction = get_extraction(session, extraction_id)
    dossier = dossiers.get(session, extraction.dossier_id)
    _require_open(dossier.status)

    _apply_field_update(
        session,
        extraction,
        expected_revision=expected_revision,
        values={
            "status": FieldStatus.CONFIRMED,
            "corrected_by": actor,
            "corrected_at": datetime.now(UTC),
            "correction_reason": reason,
        },
    )

    _record(
        session,
        dossier_id=extraction.dossier_id,
        action=DecisionAction.CONFIRM_FIELD,
        audit_action=AuditAction.FIELD_CONFIRMED,
        actor=actor,
        reason=reason,
        extraction_id=extraction.id,
        payload={"field_path": extraction.field_path, "value": extraction.value_text},
    )
    metrics.increment("iep_review_actions_total", action="confirm_field")
    return extraction


def resolve_finding(
    session: Session, finding_id: uuid.UUID, *, actor: str, reason: str, accept: bool
) -> Finding:
    finding = get_finding(session, finding_id)
    dossier = dossiers.get(session, finding.dossier_id)
    _require_open(dossier.status)

    finding.status = FindingStatus.ACCEPTED if accept else FindingStatus.DISMISSED
    finding.resolved_by = actor
    finding.resolved_at = datetime.now(UTC)
    finding.resolution_note = reason

    _record(
        session,
        dossier_id=finding.dossier_id,
        action=DecisionAction.ACCEPT_FINDING if accept else DecisionAction.DISMISS_FINDING,
        audit_action=AuditAction.FINDING_ACCEPTED if accept else AuditAction.FINDING_DISMISSED,
        actor=actor,
        reason=reason,
        finding_id=finding.id,
        payload={"rule_id": finding.rule_id, "severity": finding.severity},
    )
    metrics.increment(
        "iep_review_actions_total", action="accept_finding" if accept else "dismiss_finding"
    )
    return finding


def approve(session: Session, dossier_id: uuid.UUID, *, actor: str, reason: str) -> None:
    dossier = dossiers.get(session, dossier_id)
    _require_open(dossier.status)
    blockers = _open_blockers(session, dossier_id)
    if blockers:
        # A blocker has to be explicitly dismissed with a reason before the
        # dossier can be approved. Approving over one silently is how an
        # automated check stops meaning anything.
        raise ConflictError(
            f"{len(blockers)} blocking finding(s) are still open. Dismiss or resolve them first.",
            {"open_blockers": [str(f.id) for f in blockers]},
        )
    pending_fields = list(
        session.execute(
            select(Extraction).where(
                Extraction.dossier_id == dossier_id,
                Extraction.status == FieldStatus.NEEDS_REVIEW,
            )
        ).scalars()
    )
    if pending_fields:
        raise ConflictError(
            f"{len(pending_fields)} extracted field(s) still need review.",
            {"fields_needing_review": [str(row.id) for row in pending_fields]},
        )

    dossiers.transition(session, dossier, DossierStatus.APPROVED, actor=actor, reason=reason)
    _record(
        session,
        dossier_id=dossier_id,
        action=DecisionAction.APPROVE_DOSSIER,
        audit_action=AuditAction.DOSSIER_APPROVED,
        actor=actor,
        reason=reason,
        payload={"reference": dossier.reference},
    )
    metrics.increment("iep_review_actions_total", action="approve")


def reject(session: Session, dossier_id: uuid.UUID, *, actor: str, reason: str) -> None:
    dossier = dossiers.get(session, dossier_id)
    _require_open(dossier.status)
    dossiers.transition(session, dossier, DossierStatus.REJECTED, actor=actor, reason=reason)
    _record(
        session,
        dossier_id=dossier_id,
        action=DecisionAction.REJECT_DOSSIER,
        audit_action=AuditAction.DOSSIER_REJECTED,
        actor=actor,
        reason=reason,
        payload={"reference": dossier.reference},
    )
    metrics.increment("iep_review_actions_total", action="reject")


def _open_blockers(session: Session, dossier_id: uuid.UUID) -> list[Finding]:
    return list(
        session.execute(
            select(Finding).where(
                Finding.dossier_id == dossier_id,
                Finding.status.in_((FindingStatus.OPEN, FindingStatus.ACCEPTED)),
                Finding.severity == Severity.BLOCKER,
            )
        ).scalars()
    )


def _require_open(status: str) -> None:
    if DossierStatus(status) is not DossierStatus.NEEDS_REVIEW:
        raise ConflictError(
            "Human decisions are only accepted while the dossier is in NEEDS_REVIEW.",
            {"status": status},
        )


def _apply_field_update(
    session: Session,
    extraction: Extraction,
    *,
    expected_revision: int | None,
    values: dict[str, object],
) -> None:
    current_revision = extraction.revision
    if expected_revision is not None and expected_revision != current_revision:
        raise ConflictError(
            "The extraction changed after it was loaded. Refresh it before editing.",
            {"expected_revision": expected_revision, "actual_revision": current_revision},
        )
    applied = session.execute(
        update(Extraction)
        .where(Extraction.id == extraction.id, Extraction.revision == current_revision)
        .values(**values, revision=current_revision + 1)
        .returning(Extraction.id)
    ).scalar_one_or_none()
    if applied is None:
        session.refresh(extraction)
        raise ConflictError(
            "The extraction was edited concurrently. Refresh it before retrying.",
            {"actual_revision": extraction.revision},
        )
    session.refresh(extraction)


def _record(
    session: Session,
    *,
    dossier_id: uuid.UUID,
    action: DecisionAction,
    audit_action: AuditAction,
    actor: str,
    reason: str,
    payload: dict[str, object],
    extraction_id: uuid.UUID | None = None,
    finding_id: uuid.UUID | None = None,
    new_value: str | None = None,
) -> None:
    session.add(
        ReviewDecision(
            id=uuid.uuid4(),
            dossier_id=dossier_id,
            action=str(action),
            actor=actor,
            reason=reason,
            extraction_id=extraction_id,
            finding_id=finding_id,
            new_value=new_value,
        )
    )
    audit.record(
        session,
        action=audit_action,
        dossier_id=dossier_id,
        actor=actor,
        payload={**payload, "reason": reason},
    )
