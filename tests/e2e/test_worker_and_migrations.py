"""Worker recovery, migrations and a clean-state smoke run.

These are the tests that answer "what happens when it goes wrong in
production": a worker dies mid-job, the schema has to be created from nothing,
and a fresh database has to reach a reviewable dossier without manual steps.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from corpus.dataset import DOSSIER_B
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from iep.config import Settings
from iep.db.models import Dossier, Finding, ProcessingJob
from iep.domain.enums import DossierStatus, JobStatus
from iep.dossiers import service as dossiers
from iep.storage.local import LocalObjectStore
from iep.worker import queue
from iep.worker.runner import Worker
from tests.conftest import new_dossier
from tests.integration.test_pipeline_and_api import registry_double, scraper_double, seed

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestWorkerRecovery:
    @pytest.mark.ocr
    def test_a_job_abandoned_mid_run_is_picked_up_again(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        wired_settings: Settings,
        requires_ocr: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Simulates a worker killed while holding a job.

        The job stays RUNNING with a lease it will never renew. A second worker
        reclaims it once the lease expires, and because the pipeline is
        idempotent, starting again is safe rather than merely tolerable.
        """
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        dossiers.transition(db, dossier, DossierStatus.QUEUED)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="recover-me")
        db.commit()

        job = queue.claim(db, worker_id="worker-that-dies", lease_seconds=30)
        assert job is not None and job.status is JobStatus.RUNNING
        job.leased_until = queue.now() - timedelta(seconds=1)
        db.commit()

        # No heartbeat ever arrives. Another worker sweeps the expired lease.
        second = Worker(wired_settings, worker_id="worker-that-lives")
        monkeypatch.setattr(
            "iep.pipeline.processor.RegistryConnector",
            lambda settings: registry_double(settings),
        )
        monkeypatch.setattr(
            "iep.pipeline.processor.PublicPageScraper",
            lambda settings: scraper_double(settings),
        )
        reclaimed = second.reclaim()
        assert reclaimed == 1
        assert second.run_once() is True

        db.expire_all()
        refreshed = db.get(ProcessingJob, job.id)
        assert refreshed is not None
        assert refreshed.status is JobStatus.SUCCEEDED
        assert refreshed.leased_by is None
        assert refreshed.last_error is None
        db.refresh(dossier)
        assert DossierStatus(dossier.status) is DossierStatus.NEEDS_REVIEW

    def test_the_reclaim_is_recorded_in_the_audit_trail(
        self, db: Session, settings: Settings
    ) -> None:
        from iep.audit import service as audit

        dossier = new_dossier(db)
        dossiers.transition(db, dossier, DossierStatus.INGESTED)
        dossiers.transition(db, dossier, DossierStatus.QUEUED)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="audited")
        db.commit()
        job = queue.claim(db, worker_id="dying", lease_seconds=30)
        assert job is not None
        job.leased_until = queue.now() - timedelta(seconds=1)
        db.commit()

        Worker(settings, worker_id="reaper").reclaim()
        db.expire_all()
        assert "JOB_RECLAIMED" in {event.action for event in audit.history(db, dossier.id)}

    @pytest.mark.ocr
    def test_a_worker_processes_a_queued_dossier_end_to_end(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        wired_settings: Settings,
        requires_ocr: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        dossiers.transition(db, dossier, DossierStatus.QUEUED)
        queue.enqueue(db, dossier_id=dossier.id, idempotency_key="worker-run")
        db.commit()

        worker = Worker(wired_settings, worker_id="test-worker")
        # The external sources are doubles: the point of this test is the
        # worker loop, not the network.
        monkeypatch.setattr(
            "iep.pipeline.processor.RegistryConnector",
            lambda settings: registry_double(settings),
        )
        monkeypatch.setattr(
            "iep.pipeline.processor.PublicPageScraper",
            lambda settings: scraper_double(settings),
        )
        assert worker.run_once() is True

        db.expire_all()
        refreshed = db.get(Dossier, dossier.id)
        assert refreshed is not None
        assert DossierStatus(refreshed.status) is DossierStatus.NEEDS_REVIEW
        job = db.execute(select(ProcessingJob)).scalars().one()
        assert job.status is JobStatus.SUCCEEDED
        assert db.execute(select(Finding).where(Finding.dossier_id == dossier.id)).scalars().all()

    def test_an_empty_queue_is_a_no_op(self, wired_settings: Settings) -> None:
        assert Worker(wired_settings, worker_id="idle").run_once() is False


class TestMigrations:
    def test_the_schema_can_be_built_and_torn_down_from_nothing(
        self, database_url: str, engine: Engine
    ) -> None:
        """`alembic upgrade head` then `downgrade base`, against a real database.

        Run in a separate schema so it cannot disturb the tables the rest of
        the suite is using.
        """
        schema = "iep_migration_check"
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        env = {
            **os.environ,
            "IEP_DATABASE_URL": database_url,
            # `options` is how psycopg passes a search_path to the server.
            "PGOPTIONS": f"-c search_path={schema}",
        }
        try:
            up = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            assert up.returncode == 0, up.stderr

            with engine.connect() as connection:
                tables = connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": schema},
                ).scalars()
                names = set(tables)
            assert {"dossiers", "documents", "extractions", "findings"} <= names

            down = subprocess.run(  # noqa: S603
                [sys.executable, "-m", "alembic", "downgrade", "base"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            assert down.returncode == 0, down.stderr
        finally:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    def test_the_generated_search_column_exists(self, engine: Engine) -> None:
        with engine.connect() as connection:
            generated = connection.execute(
                text(
                    "SELECT is_generated FROM information_schema.columns "
                    "WHERE table_name = 'document_chunks' AND column_name = 'search_vector'"
                )
            ).scalar_one()
        assert generated == "ALWAYS"


@pytest.mark.ocr
class TestSmokeFromCleanState:
    def test_a_fresh_database_reaches_a_reviewable_dossier(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """The claim the README makes, executed: corpus in, review out."""
        from iep.pipeline.processor import finalise_state, process_dossier
        from iep.reporting import render
        from iep.semantic.deterministic import DeterministicSemanticExtractor

        assert db.execute(select(Dossier)).scalars().all() == []

        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        dossiers.transition(db, dossier, DossierStatus.QUEUED)
        dossiers.transition(db, dossier, DossierStatus.PROCESSING)
        result = process_dossier(
            db,
            store,
            settings,
            dossier=dossier,
            semantic=DeterministicSemanticExtractor(),
            registry=registry_double(settings),
            scraper=scraper_double(settings),
        )
        finalise_state(db, dossier)
        rendered = render.render_html(db, dossier.id)
        render.persist(db, dossier.id, rendered, report_root=settings.report_root)
        db.commit()

        assert result.documents_processed > 0
        assert result.summary.open_blockers > 0
        assert (settings.report_root).exists()
        assert list(settings.report_root.glob("*.html"))
        db.refresh(dossier)
        assert DossierStatus(dossier.status) is DossierStatus.NEEDS_REVIEW
