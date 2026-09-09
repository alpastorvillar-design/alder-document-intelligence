"""Dossier and document endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api import idempotency
from iep.api.deps import correlation, db_session, object_store, require_api_key, settings_dep
from iep.api.errors import (
    InvalidStateTransitionError,
    PayloadTooLargeError,
    UnprocessableDocumentError,
)
from iep.audit import service as audit
from iep.config import Settings
from iep.db.models import Document as DocumentRow
from iep.db.models import Dossier as DossierRow
from iep.db.models import ProcessingJob as ProcessingJobRow
from iep.domain.contracts import (
    Document,
    Dossier,
    DossierCreate,
    ProcessingJob,
    ProcessingRequest,
)
from iep.domain.enums import AuditAction, DossierStatus
from iep.dossiers import service as dossiers
from iep.ingestion.service import IngestionRejectedError, ingest_upload
from iep.observability import metrics
from iep.storage.base import ObjectStore
from iep.worker import queue

router = APIRouter(prefix="/dossiers", tags=["dossiers"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=Dossier, status_code=status.HTTP_201_CREATED)
def create_dossier(
    payload: DossierCreate,
    response: Response,
    session: Session = Depends(db_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
) -> Dossier:
    endpoint = "POST /dossiers"
    request_fp = idempotency.fingerprint(payload.model_dump(mode="json"))

    if idempotency_key:
        replay = idempotency.reserve(
            session, key=idempotency_key, endpoint=endpoint, request_fingerprint=request_fp
        )
        if replay is not None:
            response.status_code = replay.status
            return Dossier.model_validate(replay.body)

    try:
        row = dossiers.create(session, payload)
        result = Dossier.model_validate(row)
    except Exception:
        if idempotency_key:
            idempotency.release(session, key=idempotency_key, endpoint=endpoint)
            session.commit()
        raise

    if idempotency_key:
        idempotency.complete(
            session,
            key=idempotency_key,
            endpoint=endpoint,
            status=status.HTTP_201_CREATED,
            body=result.model_dump(mode="json"),
        )
    metrics.increment("iep_dossiers_created_total")
    return result


@router.get("", response_model=list[Dossier])
def list_dossiers(
    session: Session = Depends(db_session),
    dossier_status: DossierStatus | None = None,
    reference: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Dossier]:
    stmt = select(DossierRow).order_by(DossierRow.created_at.desc())
    if dossier_status is not None:
        stmt = stmt.where(DossierRow.status == dossier_status)
    if reference:
        # An integrator holds the business reference, not our id. Without this
        # they have to page the whole list and filter client-side.
        stmt = stmt.where(DossierRow.reference == reference.strip().upper())
    stmt = stmt.limit(min(limit, 200)).offset(max(offset, 0))
    return [Dossier.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get("/{dossier_id}", response_model=Dossier)
def get_dossier(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Dossier:
    return Dossier.model_validate(dossiers.get(session, dossier_id))


@router.post(
    "/{dossier_id}/documents", response_model=Document, status_code=status.HTTP_201_CREATED
)
def upload_document(
    dossier_id: uuid.UUID,
    response: Response,
    file: UploadFile = File(...),
    session: Session = Depends(db_session),
    store: ObjectStore = Depends(object_store),
    settings: Settings = Depends(settings_dep),
) -> Document:
    dossier = dossiers.get(session, dossier_id)

    # Read with a hard ceiling rather than trusting Content-Length: the limit
    # has to hold even when the header lies or is absent.
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"Uploads are limited to {settings.max_upload_bytes} bytes.",
            {"limit_bytes": settings.max_upload_bytes},
        )

    try:
        result = ingest_upload(
            session,
            store,
            settings,
            dossier=dossier,
            filename=file.filename or "unnamed",
            declared_media_type=file.content_type,
            data=data,
        )
    except IngestionRejectedError as exc:
        # The rejection is already recorded as a document row and an audit
        # event, so it is committed before the error is returned: what was
        # submitted stays visible even though the bytes were not kept.
        session.commit()
        metrics.increment("iep_documents_rejected_total", reason=str(exc.status))
        raise UnprocessableDocumentError(
            f"The file was not accepted: {exc.reason}",
            {
                "document_status": str(exc.status),
                "document_id": str(exc.document_id) if exc.document_id else None,
            },
        ) from exc

    if result.is_duplicate:
        # 200 rather than 201: nothing new was created, and the caller gets the
        # document that already holds these bytes.
        response.status_code = status.HTTP_200_OK
        metrics.increment("iep_documents_duplicate_total")
    else:
        metrics.increment("iep_documents_accepted_total", media=str(result.document.media_kind))
    return Document.model_validate(result.document)


@router.get("/{dossier_id}/documents", response_model=list[Document])
def list_documents(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> list[Document]:
    dossiers.get(session, dossier_id)
    stmt = (
        select(DocumentRow)
        .where(DocumentRow.dossier_id == dossier_id)
        .order_by(DocumentRow.received_at.asc())
    )
    return [Document.model_validate(row) for row in session.execute(stmt).scalars()]


@router.post(
    "/{dossier_id}/process", response_model=ProcessingJob, status_code=status.HTTP_202_ACCEPTED
)
def start_processing(
    dossier_id: uuid.UUID,
    response: Response,
    payload: ProcessingRequest | None = None,
    session: Session = Depends(db_session),
    settings: Settings = Depends(settings_dep),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    correlation_id: str = Depends(correlation),
) -> ProcessingJob:
    dossier = dossiers.get(session, dossier_id)

    # Without an explicit key, the key is derived from the dossier and the set
    # of documents it currently holds. Pressing "process" twice with nothing
    # changed is then a no-op, while adding a document produces a new job.
    document_digests = sorted(
        session.execute(
            select(DocumentRow.content_sha256).where(DocumentRow.dossier_id == dossier_id)
        )
        .scalars()
        .all()
    )
    if not document_digests:
        raise UnprocessableDocumentError("The dossier has no documents to process.")

    derived_key = idempotency_key or idempotency.fingerprint(
        {"dossier": str(dossier_id), "documents": document_digests}
    )

    current = DossierStatus(dossier.status)
    processable = {DossierStatus.INGESTED, DossierStatus.NEEDS_REVIEW, DossierStatus.FAILED}
    if current not in processable:
        existing = session.execute(
            select(ProcessingJobRow).where(
                ProcessingJobRow.dossier_id == dossier.id,
                ProcessingJobRow.idempotency_key == derived_key,
            )
        ).scalar_one_or_none()
        if existing is not None and current in {DossierStatus.QUEUED, DossierStatus.PROCESSING}:
            response.status_code = status.HTTP_200_OK
            return ProcessingJob.model_validate(existing)
        raise InvalidStateTransitionError(
            "Processing can only start for an ingested, review, or failed dossier.",
            {"status": str(current)},
        )

    job, created = queue.enqueue(
        session,
        dossier_id=dossier.id,
        idempotency_key=derived_key,
        payload={
            "semantic_provider": (payload.semantic_provider if payload else None),
            "correlation_id": correlation_id,
        },
        max_attempts=settings.job_max_attempts,
    )

    if created:
        dossiers.transition(session, dossier, DossierStatus.QUEUED, reason="processing requested")
        audit.record(
            session,
            action=AuditAction.PROCESSING_ENQUEUED,
            dossier_id=dossier.id,
            payload={"job_id": str(job.id), "idempotency_key": derived_key},
        )
        metrics.increment("iep_jobs_enqueued_total")
    else:
        # Replay: the same inputs already have a job, so hand back that job.
        response.status_code = status.HTTP_200_OK
        metrics.increment("iep_jobs_replayed_total")

    return ProcessingJob.model_validate(job)
