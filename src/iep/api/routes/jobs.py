"""Job inspection.

A failed job is visible with its error and attempt count rather than
disappearing; that is what makes DEAD_LETTER an inspectable state instead of a
silent drop.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.deps import db_session, require_api_key
from iep.api.errors import NotFoundError
from iep.db.models import ProcessingJob as JobRow
from iep.domain.contracts import ProcessingJob
from iep.domain.enums import JobStatus

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[ProcessingJob])
def list_jobs(
    session: Session = Depends(db_session),
    job_status: JobStatus | None = None,
    dossier_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[ProcessingJob]:
    stmt = select(JobRow).order_by(JobRow.created_at.desc()).limit(min(limit, 200))
    if job_status is not None:
        stmt = stmt.where(JobRow.status == job_status)
    if dossier_id is not None:
        stmt = stmt.where(JobRow.dossier_id == dossier_id)
    return [ProcessingJob.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get("/{job_id}", response_model=ProcessingJob)
def get_job(job_id: uuid.UUID, session: Session = Depends(db_session)) -> ProcessingJob:
    row = session.get(JobRow, job_id)
    if row is None:
        raise NotFoundError("No job with that id.", {"job_id": str(job_id)})
    return ProcessingJob.model_validate(row)
