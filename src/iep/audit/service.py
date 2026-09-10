"""Append-only audit trail.

Every event is written inside the caller's transaction. That is the point: if
the business change rolls back, so does the claim that it happened, and if it
commits, the record commits with it. No path in this codebase updates or
deletes an audit row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iep.db.models import AuditEvent
from iep.domain.enums import AuditAction
from iep.logging import current_context

SYSTEM_ACTOR = "system"


def record(
    session: Session,
    *,
    action: AuditAction,
    dossier_id: uuid.UUID | None = None,
    actor: str = SYSTEM_ACTOR,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        id=uuid.uuid4(),
        dossier_id=dossier_id,
        action=str(action),
        actor=actor,
        correlation_id=correlation_id or current_context().get("correlation_id"),
        payload=payload or {},
    )
    session.add(event)
    return event


def count_since(session: Session, action: AuditAction, *, since: datetime) -> int:
    """How many times this happened in a window, read from the trail itself.

    The trail is append-only, so it is the one place a count cannot drift from
    what actually occurred - no separate counter to keep in step, and nothing
    to reset by restarting the process.
    """
    stmt = (
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == str(action), AuditEvent.created_at >= since)
    )
    return int(session.execute(stmt).scalar_one())


def history(session: Session, dossier_id: uuid.UUID, *, limit: int = 500) -> list[AuditEvent]:
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.dossier_id == dossier_id)
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())
