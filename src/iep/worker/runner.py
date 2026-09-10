"""The worker process.

Claim a job, process it, commit. Failures are classified before they are
retried: a malformed document does not become valid on a second attempt, so
retrying it only delays real work, while a database blip or a timed-out
connector is worth another go.

Recovery is a property of the lease, not of this loop. A worker killed halfway
through leaves its job RUNNING with an expired lease; any worker - including a
fresh one - reclaims it and starts again. Because the pipeline is idempotent,
starting again is safe.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread
from types import FrameType

from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.audit import service as audit
from iep.config import Settings, get_settings
from iep.connectors.public_page import ScraperError
from iep.connectors.registry import ConnectorError
from iep.db.models import Dossier, ProcessingJob
from iep.db.session import session_scope
from iep.domain.enums import AuditAction, DossierStatus, JobStatus
from iep.dossiers import service as dossiers
from iep.extraction.base import ExtractionError
from iep.logging import configure_logging, log_context
from iep.observability import metrics
from iep.pipeline.processor import finalise_state, process_dossier
from iep.retrieval.embeddings import EmbeddingProviderError
from iep.semantic.protocol import SemanticExtractor, SemanticProviderError
from iep.storage.local import LocalObjectStore
from iep.worker import queue

log = logging.getLogger(__name__)

# Errors worth another attempt. Anything else is a property of the input.
RETRYABLE = (
    ConnectorError,
    ScraperError,
    SemanticProviderError,
    EmbeddingProviderError,
    ExtractionError,
    OSError,
)


def build_semantic_provider(settings: Settings) -> SemanticExtractor:
    if settings.semantic_provider == "llm":
        from iep.semantic.llm import AnthropicSemanticExtractor

        return AnthropicSemanticExtractor(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_attempts=settings.llm_max_attempts,
            max_output_tokens=settings.llm_max_output_tokens,
            base_url=settings.llm_base_url,
        )

    from iep.semantic.deterministic import DeterministicSemanticExtractor

    return DeterministicSemanticExtractor()


class Worker:
    def __init__(self, settings: Settings | None = None, *, worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.store = LocalObjectStore(self.settings.storage_root)
        self.semantic = build_semantic_provider(self.settings)
        self._stopping = False

    def request_stop(self, *_: object) -> None:
        # Finish the job in hand, then exit. Killing mid-job is also safe, but
        # a clean stop avoids a lease timeout for no reason.
        self._stopping = True

    def run_forever(self) -> None:
        log.info("worker_started", extra={"worker_id": self.worker_id})
        while not self._stopping:
            reclaimed = self.reclaim()
            processed = self.run_once()
            if not processed and not reclaimed:
                time.sleep(self.settings.worker_poll_seconds)
        log.info("worker_stopped", extra={"worker_id": self.worker_id})

    def reclaim(self) -> int:
        with session_scope() as session:
            expired = queue.reclaim_expired(session)
            for job_id in expired:
                job = session.get(ProcessingJob, job_id)
                if job is not None:
                    audit.record(
                        session,
                        action=AuditAction.JOB_RECLAIMED,
                        dossier_id=job.dossier_id,
                        payload={"job_id": str(job_id), "reclaimed_by": self.worker_id},
                    )
            if expired:
                metrics.increment("iep_jobs_reclaimed_total", value=len(expired))
                log.warning("jobs_reclaimed", extra={"count": len(expired)})
            return len(expired)

    def run_once(self) -> bool:
        with session_scope() as session:
            job = queue.claim(
                session, worker_id=self.worker_id, lease_seconds=self.settings.job_lease_seconds
            )
            if job is None:
                return False
            job_id = job.id
            dossier_id = job.dossier_id
            correlation_id = str(job.payload.get("correlation_id") or job_id)
            provider_override = job.payload.get("semantic_provider")

        with log_context(
            correlation_id=correlation_id,
            job_id=str(job_id),
            dossier_id=str(dossier_id),
            worker_id=self.worker_id,
        ):
            self._execute(job_id, dossier_id, provider_override)
        return True

    def _execute(self, job_id: uuid.UUID, dossier_id: uuid.UUID, provider_override: object) -> None:
        started = time.monotonic()
        semantic = self.semantic
        if (
            isinstance(provider_override, str)
            and provider_override != self.settings.semantic_provider
        ):
            overridden = self.settings.model_copy(update={"semantic_provider": provider_override})
            semantic = build_semantic_provider(overridden)

        try:
            with self._heartbeat(job_id), session_scope() as session:
                dossier = self._prepare(session, dossier_id)
                audit.record(
                    session,
                    action=AuditAction.PROCESSING_STARTED,
                    dossier_id=dossier_id,
                    payload={"job_id": str(job_id), "worker_id": self.worker_id},
                )
                result = process_dossier(
                    session,
                    self.store,
                    self.settings,
                    dossier=dossier,
                    semantic=semantic,
                )
                finalise_state(session, dossier)
                queue.succeed(session, job_id, worker_id=self.worker_id)

            metrics.observe_duration("iep_job_seconds", time.monotonic() - started)
            log.info(
                "job_succeeded",
                extra={
                    "documents": result.documents_processed,
                    "extractions": result.extractions_written,
                    "findings_open": result.summary.open_total,
                    "warnings": result.warnings,
                },
            )
        except queue.LostLeaseError:
            log.warning("job_lease_lost", extra={"job_id": str(job_id)})
        except Exception as exc:
            self._fail(job_id, dossier_id, exc)

    @contextmanager
    def _heartbeat(self, job_id: uuid.UUID) -> Iterator[None]:
        stopped = Event()
        interval = max(1.0, self.settings.job_lease_seconds / 3)

        def renew() -> None:
            while not stopped.wait(interval):
                with session_scope() as session:
                    if not queue.heartbeat(
                        session,
                        job_id,
                        worker_id=self.worker_id,
                        lease_seconds=self.settings.job_lease_seconds,
                    ):
                        log.warning("job_heartbeat_refused", extra={"job_id": str(job_id)})
                        return

        thread = Thread(target=renew, name=f"lease-{job_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=interval + 1.0)

    def _prepare(self, session: Session, dossier_id: uuid.UUID) -> Dossier:
        dossier = session.execute(select(Dossier).where(Dossier.id == dossier_id)).scalar_one()
        # QUEUED is the normal entry. A reclaimed job may find the dossier
        # already in PROCESSING, which is the state it left behind.
        if DossierStatus(dossier.status) is DossierStatus.QUEUED:
            dossiers.transition(session, dossier, DossierStatus.PROCESSING, reason="worker claimed")
        return dossier

    def _fail(self, job_id: uuid.UUID, dossier_id: uuid.UUID, exc: Exception) -> None:
        retryable = isinstance(exc, RETRYABLE) and getattr(exc, "retryable", True)
        with session_scope() as session:
            job = session.get(ProcessingJob, job_id)
            if job is None:
                return
            try:
                status = queue.fail(
                    session,
                    job,
                    error=f"{type(exc).__name__}: {exc}",
                    retryable=bool(retryable),
                    worker_id=self.worker_id,
                )
            except queue.LostLeaseError:
                log.warning(
                    "job_failure_not_recorded_after_lease_loss", extra={"job_id": str(job_id)}
                )
                return
            audit.record(
                session,
                action=AuditAction.PROCESSING_FAILED,
                dossier_id=dossier_id,
                payload={
                    "job_id": str(job_id),
                    "error_type": type(exc).__name__,
                    "retryable": bool(retryable),
                    "job_status": str(status),
                },
            )
            if status in (JobStatus.FAILED, JobStatus.DEAD_LETTER):
                dossier = session.get(Dossier, dossier_id)
                if dossier is not None:
                    dossiers.try_transition(
                        session, dossier, DossierStatus.FAILED, reason=type(exc).__name__
                    )
        metrics.increment("iep_jobs_failed_total", retryable=str(bool(retryable)).lower())
        log.exception("job_failed", extra={"job_id": str(job_id), "retryable": bool(retryable)})


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = Worker(settings)

    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_signal", extra={"signal": signum})
        worker.request_stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
