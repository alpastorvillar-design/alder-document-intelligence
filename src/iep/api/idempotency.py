"""Request-level idempotency.

An automation that retries a POST after a timeout must not create a second
dossier or a second job. The key is reserved in its own transaction before the
work starts, so two concurrent requests carrying the same key cannot both
proceed: the loser sees the reservation and is told the request is either
already done or still running.

Reusing a key with a *different* body is rejected rather than silently
answered with the old response, because that is almost always a client bug.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from iep.api.errors import ConflictError
from iep.db.models import IdempotencyRecord

IN_FLIGHT = 0


def fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Replay:
    status: int
    body: dict[str, Any]


def reserve(
    session: Session, *, key: str, endpoint: str, request_fingerprint: str
) -> Replay | None:
    """Claim `key`. Returns the stored response if this is a genuine replay."""
    stmt = (
        pg_insert(IdempotencyRecord)
        .values(
            key=key,
            endpoint=endpoint,
            request_fingerprint=request_fingerprint,
            response_status=IN_FLIGHT,
            response_body={},
        )
        .on_conflict_do_nothing(index_elements=["key", "endpoint"])
        .returning(IdempotencyRecord.key)
    )
    # RETURNING rather than rowcount: an ORM-enabled INSERT does not report a
    # suppressed conflict reliably through rowcount, and a wrong answer here
    # would either duplicate work or reject a first attempt.
    claimed = session.execute(stmt).scalar_one_or_none()
    session.commit()
    if claimed is not None:
        return None

    existing = session.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.key == key, IdempotencyRecord.endpoint == endpoint
        )
    ).scalar_one()

    if existing.request_fingerprint != request_fingerprint:
        raise ConflictError(
            "This idempotency key was already used with a different request body.",
            {"idempotency_key": key},
        )
    if existing.response_status == IN_FLIGHT:
        raise ConflictError(
            "A request with this idempotency key is still being processed.",
            {"idempotency_key": key},
        )
    return Replay(status=existing.response_status, body=existing.response_body)


def complete(
    session: Session, *, key: str, endpoint: str, status: int, body: dict[str, Any]
) -> None:
    record = session.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.key == key, IdempotencyRecord.endpoint == endpoint
        )
    ).scalar_one()
    record.response_status = status
    record.response_body = body


def release(session: Session, *, key: str, endpoint: str) -> None:
    """Drop a reservation whose work failed, so the caller can retry."""
    record = session.execute(
        select(IdempotencyRecord).where(
            IdempotencyRecord.key == key, IdempotencyRecord.endpoint == endpoint
        )
    ).scalar_one_or_none()
    if record is not None and record.response_status == IN_FLIGHT:
        session.delete(record)
