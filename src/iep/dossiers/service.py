"""Dossier lifecycle.

State changes go through `transition`, which does two things that a plain
attribute assignment does not: it refuses a move the state machine forbids,
and it applies the move as a conditional UPDATE so that two concurrent writers
cannot both believe they won.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from iep.api.errors import ConflictError, InvalidStateTransitionError, NotFoundError
from iep.audit import service as audit
from iep.db.models import Dossier
from iep.domain.contracts import DossierCreate
from iep.domain.enums import AuditAction, DossierStatus
from iep.domain.states import InvalidTransitionError, assert_transition


def create(session: Session, payload: DossierCreate) -> Dossier:
    existing = session.execute(
        select(Dossier).where(Dossier.reference == payload.reference)
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(
            "A dossier already exists for this reference.",
            {"reference": payload.reference, "dossier_id": str(existing.id)},
        )

    dossier = Dossier(
        id=uuid.uuid4(),
        reference=payload.reference,
        title=payload.title,
        period_start=payload.period_start,
        period_end=payload.period_end,
        claimed_total_eur=payload.claimed_total_eur,
        call_page_url=payload.call_page_url,
        status=DossierStatus.DRAFT,
    )
    session.add(dossier)
    session.flush()
    audit.record(
        session,
        action=AuditAction.DOSSIER_CREATED,
        dossier_id=dossier.id,
        payload={"reference": dossier.reference, "title": dossier.title},
    )
    return dossier


def get(session: Session, dossier_id: uuid.UUID) -> Dossier:
    dossier = session.get(Dossier, dossier_id)
    if dossier is None:
        raise NotFoundError("No dossier with that id.", {"dossier_id": str(dossier_id)})
    return dossier


def get_by_reference(session: Session, reference: str) -> Dossier | None:
    return session.execute(
        select(Dossier).where(Dossier.reference == reference)
    ).scalar_one_or_none()


def transition(
    session: Session,
    dossier: Dossier,
    target: DossierStatus,
    *,
    actor: str = audit.SYSTEM_ACTOR,
    reason: str | None = None,
) -> Dossier:
    current = DossierStatus(dossier.status)
    try:
        assert_transition(current, target)
    except InvalidTransitionError as exc:
        raise InvalidStateTransitionError(
            f"A dossier in {current} cannot move to {target}.",
            {"from": str(current), "to": str(target)},
        ) from exc

    # Conditional update: the WHERE clause pins the state we validated against,
    # so a concurrent writer that already moved the row makes this a no-op and
    # we raise instead of overwriting their transition.
    applied = session.execute(
        update(Dossier)
        .where(Dossier.id == dossier.id, Dossier.status == current)
        .values(status=target)
        .returning(Dossier.id)
    ).scalar_one_or_none()
    if applied is None:
        session.refresh(dossier)
        raise InvalidStateTransitionError(
            "The dossier changed state concurrently; the transition was not applied.",
            {"expected_from": str(current), "target": str(target), "actual": str(dossier.status)},
        )
    session.refresh(dossier)

    audit.record(
        session,
        action=AuditAction.STATE_CHANGED,
        dossier_id=dossier.id,
        actor=actor,
        payload={"from": str(current), "to": str(target), "reason": reason},
    )
    return dossier


def try_transition(
    session: Session,
    dossier: Dossier,
    target: DossierStatus,
    *,
    actor: str = audit.SYSTEM_ACTOR,
    reason: str | None = None,
) -> bool:
    """Transition if legal, otherwise leave the dossier alone and report it."""
    try:
        transition(session, dossier, target, actor=actor, reason=reason)
    except InvalidStateTransitionError:
        return False
    return True
