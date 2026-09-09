"""Database behaviour: constraints, queue semantics and concurrency.

These need a real PostgreSQL. SKIP LOCKED, partial-unique behaviour, conditional
updates and generated tsvector columns are the point of the tests; a SQLite
stand-in would pass without exercising any of them.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from iep.api.errors import ConflictError, InvalidStateTransitionError
from iep.config import Settings
from iep.db.models import Document, Dossier, ProcessingJob
from iep.domain.contracts import DossierCreate
from iep.domain.enums import DocumentStatus, DossierStatus, JobStatus, MediaKind, SourceKind
from iep.dossiers import service as dossiers
from iep.ingestion.service import IngestionRejectedError, ingest_upload
from iep.storage.local import LocalObjectStore
from iep.worker import queue
from tests.conftest import new_dossier

pytestmark = pytest.mark.integration

PDF = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


def a_real_pdf() -> bytes:
    from corpus import documents
    from corpus.dataset import DOSSIER_A

    return documents.technical_report(DOSSIER_A)


class TestDossierConstraints:
    def test_a_reference_is_unique(self, db: Session) -> None:
        new_dossier(db, "INN-2025-100")
        db.commit()
        with pytest.raises(ConflictError, match="already exists"):
            dossiers.create(
                db,
                DossierCreate(
                    reference="INN-2025-100",
                    title="Second",
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 12, 31),
                    claimed_total_eur=Decimal("1.00"),
                ),
            )

    def test_a_legal_transition_is_recorded_in_the_audit_trail(self, db: Session) -> None:
        from iep.audit import service as audit

        dossier = new_dossier(db)
        dossiers.transition(db, dossier, DossierStatus.INGESTED, reason="test")
        db.commit()
        actions = [event.action for event in audit.history(db, dossier.id)]
        assert "STATE_CHANGED" in actions

    def test_an_illegal_transition_is_refused(self, db: Session) -> None:
        dossier = new_dossier(db)
        with pytest.raises(InvalidStateTransitionError):
            dossiers.transition(db, dossier, DossierStatus.APPROVED)

    def test_two_writers_cannot_both_win_a_transition(self, db: Session, session_factory) -> None:  # type: ignore[no-untyped-def]
        dossier = new_dossier(db)
        dossiers.transition(db, dossier, DossierStatus.INGESTED)
        dossiers.transition(db, dossier, DossierStatus.QUEUED)
        db.commit()

        first, second = session_factory(), session_factory()
        try:
            a = first.get(Dossier, dossier.id)
            b = second.get(Dossier, dossier.id)
            assert a is not None and b is not None

            dossiers.transition(first, a, DossierStatus.PROCESSING)
            first.commit()

            # `b` still believes the dossier is QUEUED. The conditional update
            # finds no row and the transition is refused rather than applied
            # over the winner.
            with pytest.raises(InvalidStateTransitionError):
                dossiers.transition(second, b, DossierStatus.PROCESSING)
        finally:
            first.close()
            second.close()


class TestDocumentDeduplication:
    def test_an_approved_dossier_cannot_accept_more_documents(
        self, db: Session, store: LocalObjectStore, settings: Settings
    ) -> None:
        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
            DossierStatus.APPROVED,
        ):
            dossiers.transition(db, dossier, target)

        with pytest.raises(InvalidStateTransitionError, match="cannot be added"):
            ingest_upload(
                db,
                store,
                settings,
                dossier=dossier,
                filename="late.pdf",
                declared_media_type="application/pdf",
                data=a_real_pdf(),
            )

    def test_the_same_bytes_twice_is_one_document(
        self, db: Session, store: LocalObjectStore, settings: Settings
    ) -> None:
        dossier = new_dossier(db)
        data = a_real_pdf()
        first = ingest_upload(
            db,
            store,
            settings,
            dossier=dossier,
            filename="memoria.pdf",
            declared_media_type="application/pdf",
            data=data,
        )
        second = ingest_upload(
            db,
            store,
            settings,
            dossier=dossier,
            filename="memoria-copia.pdf",
            declared_media_type="application/pdf",
            data=data,
        )
        db.commit()
        assert not first.is_duplicate
        assert second.is_duplicate
        assert second.document.id == first.document.id
        assert db.execute(select(Document).where(Document.dossier_id == dossier.id)).scalars().all()

    def test_different_documents_may_share_the_same_display_name(
        self, db: Session, store: LocalObjectStore, settings: Settings
    ) -> None:
        from corpus import documents
        from corpus.dataset import DOSSIER_A, DOSSIER_B

        dossier = new_dossier(db)
        first = ingest_upload(
            db,
            store,
            settings,
            dossier=dossier,
            filename="same-name.pdf",
            declared_media_type="application/pdf",
            data=documents.technical_report(DOSSIER_A),
        )
        second = ingest_upload(
            db,
            store,
            settings,
            dossier=dossier,
            filename="same-name.pdf",
            declared_media_type="application/pdf",
            data=documents.technical_report(DOSSIER_B),
        )
        db.commit()
        assert not first.is_duplicate and not second.is_duplicate
        assert first.document.id != second.document.id
        assert first.document.original_filename == second.document.original_filename

    def test_the_constraint_stops_a_racing_second_insert(self, db: Session) -> None:
        dossier = new_dossier(db)
        digest = "a" * 64
        for _ in range(2):
            db.add(
                Document(
                    id=uuid.uuid4(),
                    dossier_id=dossier.id,
                    original_filename="x.pdf",
                    declared_media_type=None,
                    media_kind=MediaKind.PDF,
                    status=DocumentStatus.RECEIVED,
                    source_kind=SourceKind.UPLOAD,
                    source_detail=None,
                    size_bytes=1,
                    content_sha256=digest,
                    storage_key="aa/aa/" + digest,
                    page_count=1,
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_a_refused_upload_is_recorded_without_storing_the_bytes(
        self, db: Session, store: LocalObjectStore, settings: Settings
    ) -> None:
        dossier = new_dossier(db)
        with pytest.raises(IngestionRejectedError) as excinfo:
            ingest_upload(
                db,
                store,
                settings,
                dossier=dossier,
                filename="notas.txt",
                declared_media_type="text/plain",
                data=b"just some notes",
            )
        db.commit()
        document = db.get(Document, excinfo.value.document_id)
        assert document is not None
        assert document.status is DocumentStatus.UNSUPPORTED
        assert document.rejection_reason
        assert document.storage_key == "not-stored"
        assert not list(settings.storage_root.rglob("*")) or not any(
            path.is_file() for path in settings.storage_root.rglob("*")
        )


class TestJobQueue:
    def test_the_same_key_enqueues_once(self, db: Session) -> None:
        dossier = new_dossier(db)
        first, created_first = queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        second, created_second = queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        db.commit()
        assert created_first and not created_second
        assert first.id == second.id
        assert db.execute(select(ProcessingJob)).scalars().all() == [first]

    def test_the_same_key_is_scoped_to_its_dossier(self, db: Session) -> None:
        first_dossier = new_dossier(db, reference="INN-2025-901")
        second_dossier = new_dossier(db, reference="INN-2025-902")
        first, first_created = queue.enqueue(
            db, dossier_id=first_dossier.id, idempotency_key="shared"
        )
        second, second_created = queue.enqueue(
            db, dossier_id=second_dossier.id, idempotency_key="shared"
        )
        db.commit()
        assert first_created and second_created
        assert first.id != second.id
        assert first.dossier_id != second.dossier_id

    def test_a_claim_marks_the_job_running_and_leases_it(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        db.commit()

        job = queue.claim(db, worker_id="w1", lease_seconds=30)
        db.commit()
        assert job is not None
        assert job.status is JobStatus.RUNNING
        assert job.leased_by == "w1"
        assert job.attempts == 1
        assert job.leased_until is not None

    def test_a_stale_worker_cannot_complete_a_reclaimed_job(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="fenced")
        db.commit()
        job = queue.claim(db, worker_id="old-worker", lease_seconds=0)
        assert job is not None
        db.commit()
        queue.reclaim_expired(db)
        db.commit()
        replacement = queue.claim(db, worker_id="new-worker", lease_seconds=30)
        assert replacement is not None
        with pytest.raises(queue.LostLeaseError):
            queue.succeed(db, job.id, worker_id="old-worker")

    def test_two_workers_never_claim_the_same_job(self, db: Session, session_factory) -> None:  # type: ignore[no-untyped-def]
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        db.commit()

        claimed: list[uuid.UUID | None] = []
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            session = session_factory()
            try:
                barrier.wait(timeout=10)
                job = queue.claim(session, worker_id=name, lease_seconds=30)
                session.commit()
                claimed.append(job.id if job else None)
            finally:
                session.close()

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert sorted(claimed, key=lambda value: value is None)[0] is not None
        assert claimed.count(None) == 1, f"expected exactly one worker to miss, got {claimed}"

    def test_an_expired_lease_is_reclaimed(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        db.commit()
        job = queue.claim(db, worker_id="w1", lease_seconds=30)
        assert job is not None
        # Simulate a worker that died: its lease is in the past.
        job.leased_until = queue.now() - timedelta(seconds=1)
        db.commit()

        reclaimed = queue.reclaim_expired(db)
        db.commit()
        assert reclaimed == [job.id]
        db.refresh(job)
        assert job.status is JobStatus.PENDING
        assert job.leased_by is None
        assert queue.claim(db, worker_id="w2", lease_seconds=30) is not None

    def test_a_live_lease_is_not_reclaimed(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        db.commit()
        queue.claim(db, worker_id="w1", lease_seconds=300)
        db.commit()
        assert queue.reclaim_expired(db) == []

    def test_a_recoverable_failure_is_retried_with_backoff(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k", max_attempts=3)
        db.commit()
        job = queue.claim(db, worker_id="w1", lease_seconds=30)
        assert job is not None

        status = queue.fail(db, job, error="database blip", retryable=True)
        db.commit()
        db.refresh(job)
        assert status is JobStatus.PENDING
        assert job.available_at > queue.now()
        assert job.last_error == "database blip"

    def test_an_unrecoverable_failure_is_not_retried(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k", max_attempts=3)
        db.commit()
        job = queue.claim(db, worker_id="w1", lease_seconds=30)
        assert job is not None

        status = queue.fail(db, job, error="malformed document", retryable=False)
        db.commit()
        db.refresh(job)
        assert status is JobStatus.FAILED
        assert queue.claim(db, worker_id="w2", lease_seconds=30) is None

    def test_exhausted_retries_land_in_the_dead_letter_state(self, db: Session) -> None:
        dossier = new_dossier(db)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k", max_attempts=1)
        db.commit()
        job = queue.claim(db, worker_id="w1", lease_seconds=30)
        assert job is not None
        status = queue.fail(db, job, error="still failing", retryable=True)
        db.commit()
        db.refresh(job)
        assert status is JobStatus.DEAD_LETTER
        # A dead-lettered job is inspectable, not lost.
        assert job.last_error == "still failing"
        assert job.attempts == 1

    def test_a_job_scheduled_for_later_is_not_claimed_yet(self, db: Session) -> None:
        dossier = new_dossier(db)
        job, _ = queue.enqueue(db, dossier_id=dossier.id, idempotency_key="k")
        job.available_at = queue.now() + timedelta(minutes=5)
        db.commit()
        assert queue.claim(db, worker_id="w1", lease_seconds=30) is None
