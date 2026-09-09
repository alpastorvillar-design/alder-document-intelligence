"""A job queue on PostgreSQL.

Why not Redis, Celery or RabbitMQ: the queue has to be consistent with the
dossier state it drives. Enqueueing a job and moving a dossier to QUEUED in the
same transaction is free here and needs a two-phase story anywhere else. The
throughput this workload implies — jobs per minute, each taking seconds of OCR
— is orders of magnitude below what `SELECT ... FOR UPDATE SKIP LOCKED` handles
comfortably. Adding a broker would add an operational component without
removing any of the work.

The trade-offs that come with the choice are recorded in
docs/adr/0002-postgres-job-queue.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from iep.db.models import ProcessingJob
from iep.domain.enums import JobStatus, JobType


class LostLeaseError(RuntimeError):
    """The job is no longer owned by the worker attempting to mutate it."""


def now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    session: Session,
    *,
    dossier_id: uuid.UUID,
    idempotency_key: str,
    job_type: JobType = JobType.PROCESS_DOSSIER,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> tuple[ProcessingJob, bool]:
    """Insert a job, or return the existing one for this key.

    Returns `(job, created)`. The unique constraint on `idempotency_key` is
    what makes a duplicated request a no-op rather than a second run.
    """
    stmt = (
        pg_insert(ProcessingJob)
        .values(
            id=uuid.uuid4(),
            dossier_id=dossier_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            attempts=0,
            max_attempts=max_attempts,
            payload=payload or {},
            idempotency_key=idempotency_key,
            available_at=now(),
        )
        .on_conflict_do_nothing(index_elements=["dossier_id", "idempotency_key"])
        .returning(ProcessingJob.id)
    )
    inserted = session.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        session.flush()
        job = session.get(ProcessingJob, inserted)
        assert job is not None
        return job, True

    existing = session.execute(
        select(ProcessingJob).where(
            ProcessingJob.dossier_id == dossier_id,
            ProcessingJob.idempotency_key == idempotency_key,
        )
    ).scalar_one()
    return existing, False


def claim(session: Session, *, worker_id: str, lease_seconds: int) -> ProcessingJob | None:
    """Take one runnable job, or return None.

    SKIP LOCKED lets several workers poll the same table without blocking each
    other, and the lease means a worker that dies does not strand its job.
    """
    deadline = now()
    candidate = session.execute(
        select(ProcessingJob.id)
        .where(
            ProcessingJob.status == JobStatus.PENDING,
            ProcessingJob.available_at <= deadline,
        )
        .order_by(ProcessingJob.available_at.asc(), ProcessingJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()

    if candidate is None:
        return None

    session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.id == candidate)
        .values(
            status=JobStatus.RUNNING,
            attempts=ProcessingJob.attempts + 1,
            leased_by=worker_id,
            leased_until=deadline + timedelta(seconds=lease_seconds),
        )
    )
    job = session.get(ProcessingJob, candidate)
    assert job is not None
    session.refresh(job)
    return job


def heartbeat(
    session: Session,
    job_id: uuid.UUID,
    *,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    renewed = session.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status == JobStatus.RUNNING,
            ProcessingJob.leased_by == worker_id,
        )
        .values(leased_until=now() + timedelta(seconds=lease_seconds))
        .returning(ProcessingJob.id)
    ).scalar_one_or_none()
    return renewed is not None


def succeed(session: Session, job_id: uuid.UUID, *, worker_id: str) -> None:
    applied = session.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.id == job_id,
            ProcessingJob.status == JobStatus.RUNNING,
            ProcessingJob.leased_by == worker_id,
        )
        .values(status=JobStatus.SUCCEEDED, leased_by=None, leased_until=None, last_error=None)
        .returning(ProcessingJob.id)
    ).scalar_one_or_none()
    if applied is None:
        raise LostLeaseError(f"worker {worker_id} no longer owns job {job_id}")


def fail(
    session: Session,
    job: ProcessingJob,
    *,
    error: str,
    retryable: bool,
    worker_id: str | None = None,
    backoff_seconds: float = 5.0,
) -> JobStatus:
    """Record a failure, retrying only when the error is recoverable.

    A malformed document does not become valid on a second attempt, so
    retrying it wastes a worker and delays real work. A database blip does.
    """
    exhausted = job.attempts >= job.max_attempts
    if retryable and not exhausted:
        status = JobStatus.PENDING
        available_at = now() + timedelta(seconds=backoff_seconds * (2 ** (job.attempts - 1)))
    else:
        status = JobStatus.DEAD_LETTER if retryable else JobStatus.FAILED
        available_at = job.available_at

    expected_worker = worker_id or job.leased_by
    conditions = [ProcessingJob.id == job.id, ProcessingJob.status == JobStatus.RUNNING]
    if expected_worker is not None:
        conditions.append(ProcessingJob.leased_by == expected_worker)
    applied = session.execute(
        update(ProcessingJob)
        .where(*conditions)
        .values(
            status=status,
            last_error=error[:2000],
            leased_by=None,
            leased_until=None,
            available_at=available_at,
        )
        .returning(ProcessingJob.id)
    ).scalar_one_or_none()
    if applied is None:
        raise LostLeaseError(f"worker {expected_worker} no longer owns job {job.id}")
    return status


def reclaim_expired(session: Session, *, limit: int = 20) -> list[uuid.UUID]:
    """Return jobs whose lease ran out to the pending pool.

    This is the recovery path for a worker that was killed mid-job: nothing is
    lost, the job simply becomes claimable again once its lease expires.
    """
    expired = list(
        session.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.leased_until.is_not(None),
                ProcessingJob.leased_until < now(),
            )
            .order_by(ProcessingJob.leased_until.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    if not expired:
        return []

    reclaimed_at = now()
    for job in expired:
        job.status = (
            JobStatus.DEAD_LETTER if job.attempts >= job.max_attempts else JobStatus.PENDING
        )
        job.leased_by = None
        job.leased_until = None
        job.available_at = reclaimed_at
        job.last_error = "lease expired; job reclaimed"
    return [job.id for job in expired]
