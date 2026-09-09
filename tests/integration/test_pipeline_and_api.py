"""End-to-end behaviour: the pipeline, the API, review and replay.

The pipeline tests need Tesseract because the receipts really are scans. They
skip rather than fake it when it is not installed, and CI installs it.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from corpus.dataset import DOSSIER_A, DOSSIER_B
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.config import Settings
from iep.connectors.public_page import PublicPageScraper
from iep.connectors.registry import RegistryConnector
from iep.db.models import DocumentChunk, Dossier, Extraction, Finding
from iep.domain.contracts import DossierCreate
from iep.domain.enums import (
    DossierStatus,
    ExtractionMethod,
    FieldStatus,
    FindingStatus,
    Severity,
)
from iep.dossiers import service as dossiers
from iep.ingestion.service import IngestionRejectedError, ingest_upload
from iep.pipeline.processor import finalise_state, process_dossier
from iep.reporting import render
from iep.retrieval import search as retrieval
from iep.review import service as review
from iep.semantic.deterministic import DeterministicSemanticExtractor
from iep.storage.local import LocalObjectStore

pytestmark = pytest.mark.integration

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}

REGISTRY_PAGE: dict[str, Any] = {
    "contract_version": "registry/v1",
    "page": 1,
    "page_size": 50,
    "total": 0,
    "has_more": False,
    "items": [],
}

CALL_PAGE = """<html><body><dl>
<dd data-field="call-code">CALL-SYN-2025-A</dd>
<dd data-field="eligible-from">2025-01-01</dd>
<dd data-field="eligible-to">2025-12-31</dd>
<dd data-field="max-funding">400.000,00 EUR</dd>
<dd data-field="status">OPEN</dd>
</dl></body></html>"""


def registry_double(settings: Settings) -> RegistryConnector:
    """The real connector, pointed at an in-process transport."""
    from corpus.dataset import REGISTRY_PEOPLE

    payload = dict(REGISTRY_PAGE)
    payload["total"] = len(REGISTRY_PEOPLE)
    payload["items"] = [
        {
            "employee_id": p.employee_id,
            "full_name": p.full_name,
            "role": p.role,
            "hourly_rate_eur": str(p.hourly_rate_eur),
            "contract_start": p.contract_start.isoformat(),
            "contract_end": p.contract_end.isoformat() if p.contract_end else None,
        }
        for p in REGISTRY_PEOPLE
    ]
    client = httpx.Client(
        base_url="http://registry.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    return RegistryConnector(settings, client=client)


def scraper_double(settings: Settings) -> PublicPageScraper:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=CALL_PAGE))
    )
    return PublicPageScraper(settings, client=client)


def seed(
    session: Session,
    store: LocalObjectStore,
    settings: Settings,
    corpus_dir: Path,
    reference: str,
    *,
    claimed_total: Decimal,
) -> Dossier:
    dossier = dossiers.create(
        session,
        DossierCreate(
            reference=reference,
            title="Seeded",
            period_start="2025-01-01",  # type: ignore[arg-type]
            period_end="2025-12-31",  # type: ignore[arg-type]
            claimed_total_eur=claimed_total,
            call_page_url="http://localhost/convocatoria.html",
        ),
    )
    for path in sorted((corpus_dir / reference).iterdir()):
        if path.name == "ground_truth.json" or not path.is_file():
            continue
        try:
            ingest_upload(
                session,
                store,
                settings,
                dossier=dossier,
                filename=path.name,
                declared_media_type=MEDIA_TYPES.get(path.suffix.lower()),
                data=path.read_bytes(),
            )
        except IngestionRejectedError:
            continue
    session.flush()
    return dossier


def run(session: Session, store: LocalObjectStore, settings: Settings, dossier: Dossier):  # type: ignore[no-untyped-def]
    dossiers.transition(session, dossier, DossierStatus.QUEUED)
    dossiers.transition(session, dossier, DossierStatus.PROCESSING)
    result = process_dossier(
        session,
        store,
        settings,
        dossier=dossier,
        semantic=DeterministicSemanticExtractor(),
        registry=registry_double(settings),
        scraper=scraper_double(settings),
    )
    finalise_state(session, dossier)
    session.commit()
    return result


@pytest.mark.ocr
class TestPipeline:
    def test_a_consistent_dossier_produces_no_findings(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_A.reference,
            claimed_total=DOSSIER_A.claimed_total_eur,
        )
        result = run(db, store, settings, dossier)
        open_findings = [
            f
            for f in db.execute(select(Finding).where(Finding.dossier_id == dossier.id)).scalars()
            if f.status is FindingStatus.OPEN
        ]
        assert open_findings == [], [f.rule_id for f in open_findings]
        assert result.documents_failed == 0
        assert result.extractions_written > 0

    def test_processing_never_approves(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_A.reference,
            claimed_total=DOSSIER_A.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        db.refresh(dossier)
        assert DossierStatus(dossier.status) is DossierStatus.NEEDS_REVIEW

    def test_every_seeded_defect_is_found(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        found = {
            f.rule_id
            for f in db.execute(select(Finding).where(Finding.dossier_id == dossier.id)).scalars()
        }
        missing = set(DOSSIER_B.expected_findings) - found
        assert missing == set(), f"rules that did not fire: {sorted(missing)}"

    def test_every_extraction_carries_a_locator_and_a_version(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_A.reference,
            claimed_total=DOSSIER_A.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        rows = list(
            db.execute(select(Extraction).where(Extraction.dossier_id == dossier.id)).scalars()
        )
        assert rows
        for row in rows:
            assert row.locator.get("kind")
            assert row.extractor_version
            assert row.contract_version == "1.0.0"
            assert 0.0 <= float(row.confidence) <= 1.0
        assert any(row.method is ExtractionMethod.HTTP_API for row in rows)
        assert any(row.locator.get("kind") == "API_FIELD" for row in rows)
        assert any(row.method is ExtractionMethod.HTML_SELECTOR for row in rows)
        assert any(row.locator.get("kind") == "HTML_SELECTOR" for row in rows)

    def test_no_amount_a_rule_uses_came_from_the_semantic_provider(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """The guarantee that makes document prompt injection inert.

        The adversarial annex is in this dossier. Whatever a semantic provider
        proposes, no extraction is ever written with a semantic method, so no
        rule can compare against something a model produced.
        """
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        methods = {
            row.method
            for row in db.execute(
                select(Extraction).where(Extraction.dossier_id == dossier.id)
            ).scalars()
        }
        assert ExtractionMethod.SEMANTIC_LLM not in methods
        assert ExtractionMethod.SEMANTIC_DETERMINISTIC not in methods

    def test_the_injection_document_is_reported_and_changes_nothing(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        db.refresh(dossier)
        findings = {
            f.rule_id: f
            for f in db.execute(select(Finding).where(Finding.dossier_id == dossier.id)).scalars()
        }
        assert "PROMPT_INJECTION_ATTEMPT" in findings
        assert findings["PROMPT_INJECTION_ATTEMPT"].severity is Severity.WARNING
        assert DossierStatus(dossier.status) is DossierStatus.NEEDS_REVIEW

    def test_replaying_the_pipeline_changes_nothing(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        first = run(db, store, settings, dossier)

        def state() -> tuple[tuple[object, ...], ...]:
            extraction_state = tuple(
                (
                    "extraction",
                    str(row.id),
                    row.dedup_key,
                    row.field_path,
                    row.value_text,
                    str(row.value_number),
                    str(row.value_date),
                    json.dumps(row.locator, sort_keys=True),
                    str(row.status),
                )
                for row in db.execute(
                    select(Extraction)
                    .where(Extraction.dossier_id == dossier.id)
                    .order_by(Extraction.dedup_key)
                ).scalars()
            )
            finding_state = tuple(
                (
                    "finding",
                    str(row.id),
                    row.fingerprint,
                    row.rule_id,
                    str(row.status),
                    json.dumps(row.detail, sort_keys=True),
                    tuple(row.extraction_ids),
                    tuple(row.document_ids),
                )
                for row in db.execute(
                    select(Finding)
                    .where(Finding.dossier_id == dossier.id)
                    .order_by(Finding.fingerprint)
                ).scalars()
            )
            chunk_state = tuple(
                (
                    "chunk",
                    str(row.id),
                    str(row.document_id),
                    row.ordinal,
                    row.text,
                    json.dumps(row.locator, sort_keys=True),
                )
                for row in db.execute(
                    select(DocumentChunk)
                    .where(DocumentChunk.dossier_id == dossier.id)
                    .order_by(DocumentChunk.document_id, DocumentChunk.ordinal)
                ).scalars()
            )
            return extraction_state + finding_state + chunk_state

        def counts() -> tuple[int, int, int]:
            return (
                len(
                    list(
                        db.execute(
                            select(Extraction).where(Extraction.dossier_id == dossier.id)
                        ).scalars()
                    )
                ),
                len(
                    list(
                        db.execute(
                            select(Finding).where(Finding.dossier_id == dossier.id)
                        ).scalars()
                    )
                ),
                len(
                    list(
                        db.execute(
                            select(DocumentChunk).where(DocumentChunk.dossier_id == dossier.id)
                        ).scalars()
                    )
                ),
            )

        def dedup_keys() -> set[str]:
            return {
                row.dedup_key
                for row in db.execute(
                    select(Extraction).where(Extraction.dossier_id == dossier.id)
                ).scalars()
            }

        before = counts()
        keys_before = dedup_keys()
        state_before = state()
        second = run(db, store, settings, dossier)
        assert counts() == before
        # Comparing keys, not only counts: a derived value keyed on the order
        # of its inputs would keep the count stable in one session and drift in
        # another, which is exactly how this was missed once.
        assert dedup_keys() == keys_before
        assert state() == state_before
        assert second.summary.created == 0
        assert second.extractions_written == first.extractions_written

    def test_a_correction_survives_reprocessing(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        target = db.execute(
            select(Extraction)
            .where(
                Extraction.dossier_id == dossier.id, Extraction.field_path == "invoice.total_eur"
            )
            .limit(1)
        ).scalar_one()
        review.correct_field(
            db, target.id, actor="a.reviewer", reason="read from the scan", new_value="1.234,56"
        )
        db.commit()

        run(db, store, settings, dossier)
        db.refresh(target)
        assert target.status is FieldStatus.CORRECTED
        assert target.value_number == Decimal("1234.56")
        assert target.original_value_text is not None

    def test_evidence_lookup_finds_the_period_line(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_A.reference,
            claimed_total=DOSSIER_A.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        hits = retrieval.search(db, dossier.id, "periodo de ejecucion", limit=3)
        assert hits
        assert hits[0].locator.get("kind")
        # Deterministic ordering: the same query returns the same list.
        assert [h.chunk_id for h in hits] == [
            h.chunk_id for h in retrieval.search(db, dossier.id, "periodo de ejecucion", limit=3)
        ]

    def test_the_report_shows_evidence_and_the_export_is_safe(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        rendered = render.render_html(db, dossier.id)
        html = rendered.html.decode("utf-8")
        assert "Where it came from" in html
        assert "page 1" in html or "cell" in html
        assert rendered.blocker_count > 0

        csv_bytes = render.export_csv(db, dossier.id).decode("utf-8")
        for line in csv_bytes.splitlines()[1:]:
            first_cell = line.split(",")[0]
            assert not first_cell.startswith(("=", "+", "@")), first_cell

        payload = render.export_json(db, dossier.id)
        assert b'"contract_version": "1.0.0"' in payload


class TestReviewFlow:
    def test_a_correction_keeps_the_original_reading(self, db: Session) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        extraction = Extraction(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            document_id=None,
            field_path="invoice.total_eur",
            value_text="1.234,00",
            value_number=Decimal("1234.00"),
            value_date=None,
            locator={"kind": "PDF_PAGE", "page": 1},
            method=ExtractionMethod.OCR_TESSERACT,
            extractor_version="test/1",
            contract_version="1.0.0",
            confidence=Decimal("0.60"),
            status=FieldStatus.NEEDS_REVIEW,
            dedup_key=uuid.uuid4().hex,
        )
        db.add(extraction)
        db.flush()

        review.correct_field(
            db, extraction.id, actor="a.reviewer", reason="misread", new_value="4.321,00"
        )
        db.commit()
        db.refresh(extraction)
        assert extraction.original_value_text == "1.234,00"
        assert extraction.value_number == Decimal("4321.00")
        assert float(extraction.confidence) == 1.0
        assert extraction.corrected_by == "a.reviewer"

    def test_a_second_correction_does_not_overwrite_the_original(self, db: Session) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        extraction = Extraction(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            document_id=None,
            field_path="invoice.total_eur",
            value_text="first",
            value_number=None,
            value_date=None,
            locator={"kind": "PDF_PAGE", "page": 1},
            method=ExtractionMethod.OCR_TESSERACT,
            extractor_version="test/1",
            contract_version="1.0.0",
            confidence=Decimal("0.60"),
            status=FieldStatus.NEEDS_REVIEW,
            dedup_key=uuid.uuid4().hex,
        )
        db.add(extraction)
        db.flush()
        review.correct_field(db, extraction.id, actor="a", reason="r1", new_value="second")
        review.correct_field(db, extraction.id, actor="b", reason="r2", new_value="third")
        db.commit()
        db.refresh(extraction)
        assert extraction.original_value_text == "first"
        assert extraction.value_text == "third"

    def test_concurrent_corrections_do_not_silently_overwrite(
        self, db: Session, session_factory
    ) -> None:  # type: ignore[no-untyped-def]
        from iep.api.errors import ConflictError
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        extraction = Extraction(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            document_id=None,
            field_path="invoice.total_eur",
            value_text="first",
            value_number=None,
            value_date=None,
            locator={"kind": "PDF_PAGE", "page": 1},
            method=ExtractionMethod.OCR_TESSERACT,
            extractor_version="test/1",
            contract_version="1.0.0",
            confidence=Decimal("0.60"),
            status=FieldStatus.NEEDS_REVIEW,
            dedup_key=uuid.uuid4().hex,
        )
        db.add(extraction)
        db.commit()

        first = session_factory()
        second = session_factory()
        try:
            first_row = first.get(Extraction, extraction.id)
            second_row = second.get(Extraction, extraction.id)
            assert first_row is not None and second_row is not None
            review.correct_field(
                first,
                first_row.id,
                actor="reviewer-one",
                reason="source checked",
                new_value="second",
                expected_revision=0,
            )
            first.commit()
            with pytest.raises(ConflictError, match="edited concurrently"):
                review.correct_field(
                    second,
                    second_row.id,
                    actor="reviewer-two",
                    reason="different reading",
                    new_value="third",
                    expected_revision=0,
                )
        finally:
            first.close()
            second.close()

    def test_approval_is_refused_while_a_blocker_is_open(self, db: Session) -> None:
        from iep.api.errors import ConflictError
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        dossiers.transition(db, dossier, DossierStatus.INGESTED)
        dossiers.transition(db, dossier, DossierStatus.QUEUED)
        dossiers.transition(db, dossier, DossierStatus.PROCESSING)
        dossiers.transition(db, dossier, DossierStatus.NEEDS_REVIEW)
        db.add(
            Finding(
                id=uuid.uuid4(),
                dossier_id=dossier.id,
                rule_id="TEST_BLOCKER",
                rule_version="1.0.0",
                severity=Severity.BLOCKER,
                status=FindingStatus.OPEN,
                message="blocked",
                detail={},
                extraction_ids=[],
                document_ids=[],
                fingerprint=uuid.uuid4().hex,
            )
        )
        db.flush()

        with pytest.raises(ConflictError, match="blocking finding"):
            review.approve(db, dossier.id, actor="a.reviewer", reason="looks fine")

    def test_accepting_a_blocker_does_not_make_it_approvable(self, db: Session) -> None:
        from iep.api.errors import ConflictError
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        finding = Finding(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            rule_id="TEST_BLOCKER",
            rule_version="1.0.0",
            severity=Severity.BLOCKER,
            status=FindingStatus.OPEN,
            message="blocked",
            detail={},
            extraction_ids=[],
            document_ids=[],
            fingerprint=uuid.uuid4().hex,
        )
        db.add(finding)
        db.flush()

        review.resolve_finding(
            db, finding.id, actor="a.reviewer", reason="confirmed as a real issue", accept=True
        )
        with pytest.raises(ConflictError, match="blocking finding"):
            review.approve(db, dossier.id, actor="a.reviewer", reason="cannot waive an issue")

    def test_approval_is_refused_while_a_field_needs_review(self, db: Session) -> None:
        from iep.api.errors import ConflictError
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        extraction = Extraction(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            document_id=None,
            field_path="invoice.total_eur",
            value_text="100.00",
            value_number=Decimal("100.00"),
            value_date=None,
            locator={"kind": "PDF_PAGE", "page": 1},
            method=ExtractionMethod.OCR_TESSERACT,
            extractor_version="test/1",
            contract_version="1.0.0",
            confidence=Decimal("0.60"),
            status=FieldStatus.NEEDS_REVIEW,
            dedup_key=uuid.uuid4().hex,
        )
        db.add(extraction)
        db.flush()

        with pytest.raises(ConflictError, match="still need review"):
            review.approve(db, dossier.id, actor="a.reviewer", reason="not complete")

    def test_approval_succeeds_once_the_blocker_is_resolved(self, db: Session) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        finding = Finding(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            rule_id="TEST_BLOCKER",
            rule_version="1.0.0",
            severity=Severity.BLOCKER,
            status=FindingStatus.OPEN,
            message="blocked",
            detail={},
            extraction_ids=[],
            document_ids=[],
            fingerprint=uuid.uuid4().hex,
        )
        db.add(finding)
        db.flush()

        review.resolve_finding(
            db, finding.id, actor="a.reviewer", reason="checked with the client", accept=False
        )
        review.approve(db, dossier.id, actor="a.reviewer", reason="reviewed")
        db.commit()
        db.refresh(dossier)
        assert DossierStatus(dossier.status) is DossierStatus.APPROVED

    def test_an_approved_dossier_cannot_be_edited(self, db: Session) -> None:
        from iep.api.errors import ConflictError
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        for target in (
            DossierStatus.INGESTED,
            DossierStatus.QUEUED,
            DossierStatus.PROCESSING,
            DossierStatus.NEEDS_REVIEW,
        ):
            dossiers.transition(db, dossier, target)
        review.approve(db, dossier.id, actor="a", reason="done")
        db.flush()

        extraction = Extraction(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            document_id=None,
            field_path="x",
            value_text="v",
            value_number=None,
            value_date=None,
            locator={"kind": "PDF_PAGE", "page": 1},
            method=ExtractionMethod.PDF_TEXT,
            extractor_version="t",
            contract_version="1.0.0",
            confidence=Decimal("0.9"),
            status=FieldStatus.EXTRACTED,
            dedup_key=uuid.uuid4().hex,
        )
        db.add(extraction)
        db.flush()
        with pytest.raises(ConflictError, match="only accepted"):
            review.correct_field(db, extraction.id, actor="a", reason="r", new_value="v2")


class TestApi:
    @pytest.fixture
    def client(self, wired_settings: Settings, db: Session) -> Iterator[TestClient]:  # type: ignore[name-defined]
        from iep.api.app import create_app

        with TestClient(create_app()) as client:
            yield client

    def test_creating_a_dossier_twice_with_one_key_creates_one(self, client: TestClient) -> None:
        body = {
            "reference": "INN-2025-500",
            "title": "Idempotent",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "claimed_total_eur": "100.00",
        }
        first = client.post("/dossiers", json=body, headers={"Idempotency-Key": "abc"})
        second = client.post("/dossiers", json=body, headers={"Idempotency-Key": "abc"})
        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert len(client.get("/dossiers").json()) == 1

    def test_reusing_a_key_with_a_different_body_is_refused(self, client: TestClient) -> None:
        body = {
            "reference": "INN-2025-501",
            "title": "First",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "claimed_total_eur": "100.00",
        }
        client.post("/dossiers", json=body, headers={"Idempotency-Key": "same"})
        response = client.post(
            "/dossiers",
            json={**body, "reference": "INN-2025-502"},
            headers={"Idempotency-Key": "same"},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "conflict"

    def test_two_concurrent_requests_with_one_key_create_one_dossier(
        self, client: TestClient
    ) -> None:
        body = {
            "reference": "INN-2025-503",
            "title": "Racing",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "claimed_total_eur": "100.00",
        }
        statuses: list[int] = []
        barrier = threading.Barrier(2)

        def send() -> None:
            barrier.wait(timeout=10)
            statuses.append(
                client.post("/dossiers", json=body, headers={"Idempotency-Key": "race"}).status_code
            )

        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert len(client.get("/dossiers").json()) == 1
        # One wins; the other is told the request is in flight or replayed.
        assert 201 in statuses
        assert sorted(statuses) in ([201, 201], [201, 409])

    def test_an_error_never_leaks_a_traceback(self, client: TestClient) -> None:
        response = client.get(f"/dossiers/{uuid.uuid4()}")
        assert response.status_code == 404
        body = response.json()
        assert set(body) == {"error", "message", "correlation_id", "detail"}
        assert "Traceback" not in response.text

    def test_the_correlation_id_is_echoed(self, client: TestClient) -> None:
        response = client.get("/healthz", headers={"X-Correlation-ID": "trace-me"})
        assert response.headers["X-Correlation-ID"] == "trace-me"

    def test_an_unsupported_upload_is_refused_with_a_reason(self, client: TestClient) -> None:
        created = client.post(
            "/dossiers",
            json={
                "reference": "INN-2025-504",
                "title": "Uploads",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "claimed_total_eur": "100.00",
            },
        ).json()
        response = client.post(
            f"/dossiers/{created['id']}/documents",
            files={"file": ("notes.txt", b"plain text", "text/plain")},
        )
        assert response.status_code == 422
        assert response.json()["detail"]["document_status"] == "UNSUPPORTED"
        # The refusal is still visible as part of the dossier.
        documents = client.get(f"/dossiers/{created['id']}/documents").json()
        assert [d["status"] for d in documents] == ["UNSUPPORTED"]

    def test_processing_without_documents_is_refused(self, client: TestClient) -> None:
        created = client.post(
            "/dossiers",
            json={
                "reference": "INN-2025-505",
                "title": "Empty",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "claimed_total_eur": "100.00",
            },
        ).json()
        response = client.post(f"/dossiers/{created['id']}/process")
        assert response.status_code == 422

    def test_the_openapi_document_describes_the_error_shape(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "ApiError" in schema["components"]["schemas"]
        assert "/dossiers/{dossier_id}/approve" in schema["paths"]


class TestReviewPages:
    """The URLs the README and the demo script actually print.

    A route that exists at a different path from the one every document points
    at is a broken product, however well the handler works.
    """

    @pytest.fixture
    def client(self, wired_settings: Settings, db: Session) -> Iterator[TestClient]:
        from iep.api.app import create_app

        with TestClient(create_app()) as client:
            yield client

    def test_the_documented_review_queue_url_serves_the_list(self, client: TestClient) -> None:
        response = client.get("/ui/dossiers")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Dossiers" in response.text

    def test_the_bare_ui_path_redirects_to_the_list(self, client: TestClient) -> None:
        response = client.get("/ui", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/ui/dossiers"

    def test_one_dossier_can_be_opened_for_review(self, client: TestClient) -> None:
        created = client.post(
            "/dossiers",
            json={
                "reference": "INN-2025-600",
                "title": "Review page",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "claimed_total_eur": "100.00",
            },
        ).json()
        response = client.get(f"/ui/dossiers/{created['id']}")
        assert response.status_code == 200
        assert "Review page" in response.text
