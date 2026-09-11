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
from corpus.dataset import DOSSIER_A, DOSSIER_B, DOSSIERS, DossierSpec
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api import vocabulary as vocab
from iep.config import Settings
from iep.connectors.public_page import PublicPageScraper
from iep.connectors.registry import RegistryConnector
from iep.db.models import Document, DocumentChunk, Dossier, Extraction, Finding
from iep.domain.contracts import DossierCreate
from iep.domain.enums import (
    DocumentKind,
    DocumentStatus,
    DossierStatus,
    ExtractionMethod,
    FieldStatus,
    FindingStatus,
    MediaKind,
    Severity,
    SourceKind,
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

    @pytest.mark.parametrize("spec", DOSSIERS, ids=lambda spec: spec.reference)
    def test_each_dossier_produces_exactly_the_findings_it_was_built_for(
        self,
        spec: DossierSpec,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """Both directions matter, and the second one is the harder claim.

        Missing rules mean a defect walked past. Extra rules mean a false
        positive, and a reviewer who is shown findings that are not real stops
        reading the ones that are - so "0 false positives" is only worth
        stating if something asserts it.
        """
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            spec.reference,
            claimed_total=spec.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        found = {
            f.rule_id
            for f in db.execute(select(Finding).where(Finding.dossier_id == dossier.id)).scalars()
        }
        expected = set(spec.expected_findings)
        assert found - expected == set(), f"false positives: {sorted(found - expected)}"
        assert expected - found == set(), f"rules that did not fire: {sorted(expected - found)}"

    def test_the_corpus_exercises_the_rules_it_claims_to(self) -> None:
        """Three rules were in the catalogue with nothing to make them fire.

        A rule covered only by a unit test has never met a document: it has
        never been through ingestion, extraction and aggregation. This pins the
        coverage so it cannot quietly shrink again.
        """
        seeded = {rule_id for spec in DOSSIERS for rule_id in spec.expected_findings}
        for rule_id in (
            "CLAIM_ABOVE_CALL_MAXIMUM",
            "HOURS_ABOVE_ANNUAL_CEILING",
            "PROJECT_CODE_MISMATCH",
        ):
            assert rule_id in seeded, f"{rule_id} has no document that makes it fire"

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
        # The filed report names the column the same way the screen does.
        assert "Procedencia" in html
        assert "página 1" in html or "celda" in html
        assert rendered.blocker_count > 0

        csv_bytes = render.export_csv(db, dossier.id).decode("utf-8")
        for line in csv_bytes.splitlines()[1:]:
            first_cell = line.split(",")[0]
            assert not first_cell.startswith(("=", "+", "@")), first_cell

        # The same content in the shape a double-click into Excel needs. Both
        # are produced from one writer, so the neutralisation above cannot
        # apply to only one of them.
        import csv as csv_module
        import io as io_module

        excel_bytes = render.export_csv(db, dossier.id, dialect="excel")
        assert excel_bytes.startswith(b"\xef\xbb\xbf")
        standard_rows = list(csv_module.reader(io_module.StringIO(csv_bytes), delimiter=","))
        excel_rows = list(
            csv_module.reader(io_module.StringIO(excel_bytes.decode("utf-8-sig")), delimiter=";")
        )
        assert excel_rows == standard_rows
        assert len(excel_rows[0]) == 10

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
        assert "Expedientes pendientes" in response.text

    def test_the_intake_screen_can_actually_receive_files(self, client: TestClient) -> None:
        """Without a file input the only way in is the CLI or Swagger.

        A reviewer is not going to use either, so the presence of the upload
        control is part of the product working, not a cosmetic detail.
        """
        response = client.get("/ui/dossiers/new")
        assert response.status_code == 200
        assert 'type="file"' in response.text
        assert "/dossiers/${dossier.id}/documents" in response.text

    def test_new_is_not_read_as_a_dossier_id(self, client: TestClient) -> None:
        """`/dossiers/new` and `/dossiers/{uuid}` share a prefix.

        Declaration order decides, so a reordering that broke this would give a
        422 on the intake screen rather than an obvious error.
        """
        assert client.get("/ui/dossiers/new").status_code == 200

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


class TestEvidenceViewer:
    """A locator is only worth storing if it can be walked backwards.

    These drive the viewer with a real scan and a real workbook out of the
    corpus, so a coordinate that no longer lands on the page shows up here
    rather than in front of a reviewer.
    """

    @pytest.fixture
    def client(self, wired_settings: Settings, db: Session) -> Iterator[TestClient]:
        from iep.api.app import create_app

        with TestClient(create_app()) as client:
            yield client

    def _document(
        self,
        db: Session,
        store: LocalObjectStore,
        dossier_id: uuid.UUID,
        path: Path,
        media: MediaKind,
    ) -> Document:
        import hashlib

        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        row = Document(
            id=uuid.uuid4(),
            dossier_id=dossier_id,
            original_filename=path.name,
            declared_media_type=None,
            media_kind=media,
            document_kind=DocumentKind.EXPENSE_INVOICE,
            status=DocumentStatus.EXTRACTED,
            source_kind=SourceKind.UPLOAD,
            size_bytes=len(data),
            content_sha256=digest,
            storage_key=store.put(digest, data),
            page_count=1,
        )
        db.add(row)
        db.flush()
        return row

    def _extraction(
        self, db: Session, dossier_id: uuid.UUID, document_id: uuid.UUID, locator: dict[str, Any]
    ) -> Extraction:
        row = Extraction(
            id=uuid.uuid4(),
            dossier_id=dossier_id,
            document_id=document_id,
            field_path="invoice.total_eur",
            value_text="18.392,00",
            value_number=Decimal("18392.00"),
            value_date=None,
            locator=locator,
            method=ExtractionMethod.OCR_TESSERACT,
            extractor_version="test/1",
            contract_version="1.0.0",
            confidence=Decimal("0.64"),
            status=FieldStatus.NEEDS_REVIEW,
            dedup_key=uuid.uuid4().hex,
        )
        db.add(row)
        db.flush()
        return row

    def test_a_scan_is_served_with_the_box_the_engine_reported(
        self,
        client: TestClient,
        db: Session,
        store: LocalObjectStore,
        corpus_dir: Path,
    ) -> None:
        from tests.conftest import new_dossier

        scan = next((corpus_dir / DOSSIER_B.reference).glob("justificante-01-*.jpg"))
        dossier = new_dossier(db)
        document = self._document(db, store, dossier.id, scan, MediaKind.JPEG)
        extraction = self._extraction(
            db,
            dossier.id,
            document.id,
            {
                "kind": "OCR_WORD_BOX",
                "page": 1,
                "left": 240,
                "top": 800,
                "width": 512,
                "height": 28,
                "word_confidence": 64.0,
                "snippet": "TOTAL FACTURA",
            },
        )
        db.commit()

        page = client.get(f"/ui/evidence/{extraction.id}")
        assert page.status_code == 200
        # The box is positioned as a percentage of the image, so it survives
        # the browser scaling the page down to fit.
        assert 'class="mark"' in page.text
        assert "left:" in page.text
        assert "Confianza del OCR" in page.text

        image = client.get(f"/ui/evidence/{extraction.id}/image")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/jpeg"
        assert image.content == scan.read_bytes()

    def test_a_workbook_cell_is_shown_with_its_neighbours(
        self,
        client: TestClient,
        db: Session,
        store: LocalObjectStore,
        corpus_dir: Path,
    ) -> None:
        """A cell reference means nothing without the row it sits in."""
        from tests.conftest import new_dossier

        workbook = (corpus_dir / DOSSIER_B.reference) / "partes-horarios.xlsx"
        dossier = new_dossier(db)
        document = self._document(db, store, dossier.id, workbook, MediaKind.XLSX)
        extraction = self._extraction(
            db,
            dossier.id,
            document.id,
            {
                "kind": "EXCEL_CELL",
                "sheet": "Partes horarios",
                "cell": "E7",
                "row": 7,
                "column": "E",
            },
        )
        db.commit()

        page = client.get(f"/ui/evidence/{extraction.id}")
        assert page.status_code == 200
        assert 'class="grid"' in page.text
        # The target cell is marked, and the header row travels with it.
        assert page.text.count('class="t"') == 1

    def test_a_derived_total_lists_the_values_it_was_computed_from(
        self, client: TestClient, db: Session
    ) -> None:
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        part = self._extraction(db, dossier.id, None, {"kind": "PDF_PAGE", "page": 1})
        total = self._extraction(
            db,
            dossier.id,
            None,
            {"kind": "DERIVED", "inputs": [str(part.id)], "rule": "sum_of_invoice_totals"},
        )
        db.commit()

        page = client.get(f"/ui/evidence/{total.id}")
        assert page.status_code == 200
        assert "Valores que se han sumado" in page.text
        assert f"/ui/evidence/{part.id}" in page.text

    def test_an_unknown_extraction_is_a_clean_404(self, client: TestClient) -> None:
        response = client.get(f"/ui/evidence/{uuid.uuid4()}")
        assert response.status_code == 404
        assert "Traceback" not in response.text


class TestTheOriginalDocumentIsWhatOpens:
    """The rasterised copy is for drawing a box on, not for opening.

    The viewer renders a PDF page to PNG so the stored coordinates can be
    placed on it. "Open the whole document" pointed at that PNG, so a reviewer
    who wanted the PDF got a picture of page one, and a workbook had nothing to
    open at all.
    """

    @pytest.fixture
    def client(self, wired_settings: Settings, db: Session) -> Iterator[TestClient]:
        from iep.api.app import create_app

        with TestClient(create_app()) as client:
            yield client

    def _stored(
        self,
        db: Session,
        store: LocalObjectStore,
        dossier_id: uuid.UUID,
        path: Path,
        media: MediaKind,
    ) -> Document:
        import hashlib

        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        row = Document(
            id=uuid.uuid4(),
            dossier_id=dossier_id,
            original_filename=path.name,
            declared_media_type=None,
            media_kind=media,
            document_kind=DocumentKind.UNKNOWN,
            status=DocumentStatus.EXTRACTED,
            source_kind=SourceKind.UPLOAD,
            size_bytes=len(data),
            content_sha256=digest,
            storage_key=store.put(digest, data),
            page_count=1,
        )
        db.add(row)
        db.flush()
        return row

    def test_a_pdf_is_served_as_a_pdf_byte_for_byte(
        self, client: TestClient, db: Session, store: LocalObjectStore, corpus_dir: Path
    ) -> None:
        from tests.conftest import new_dossier

        source = (corpus_dir / DOSSIER_B.reference) / "memoria-tecnica.pdf"
        dossier = new_dossier(db)
        document = self._stored(db, store, dossier.id, source, MediaKind.PDF)
        db.commit()

        response = client.get(f"/ui/documents/{document.id}/original")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        # Inline, so the browser's own PDF viewer opens it rather than
        # downloading a file the reviewer then has to find.
        assert response.headers["content-disposition"].startswith("inline")
        assert "memoria-tecnica.pdf" in response.headers["content-disposition"]
        assert response.content == source.read_bytes()
        assert response.content.startswith(b"%PDF-")

    def test_a_workbook_is_sent_as_a_download_so_excel_opens_it(
        self, client: TestClient, db: Session, store: LocalObjectStore, corpus_dir: Path
    ) -> None:
        from tests.conftest import new_dossier

        source = (corpus_dir / DOSSIER_B.reference) / "partes-horarios.xlsx"
        dossier = new_dossier(db)
        document = self._stored(db, store, dossier.id, source, MediaKind.XLSX)
        db.commit()

        response = client.get(f"/ui/documents/{document.id}/original")
        assert response.status_code == 200
        assert "spreadsheetml.sheet" in response.headers["content-type"]
        assert response.headers["content-disposition"].startswith("attachment")
        assert response.content == source.read_bytes()

    def test_a_scan_keeps_its_own_bytes(
        self, client: TestClient, db: Session, store: LocalObjectStore, corpus_dir: Path
    ) -> None:
        from tests.conftest import new_dossier

        source = next((corpus_dir / DOSSIER_B.reference).glob("justificante-01-*.jpg"))
        dossier = new_dossier(db)
        document = self._stored(db, store, dossier.id, source, MediaKind.JPEG)
        db.commit()

        response = client.get(f"/ui/documents/{document.id}/original")
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == source.read_bytes()

    def test_a_refused_document_has_no_bytes_to_open(self, client: TestClient, db: Session) -> None:
        """Nothing unparsable is stored, so this is a 404 and not an empty file."""
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        row = Document(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            original_filename="notas-internas.txt",
            declared_media_type="text/plain",
            media_kind=MediaKind.UNSUPPORTED,
            document_kind=DocumentKind.UNKNOWN,
            status=DocumentStatus.UNSUPPORTED,
            source_kind=SourceKind.UPLOAD,
            size_bytes=10,
            content_sha256="f" * 64,
            storage_key="",
            page_count=None,
            rejection_reason="la firma del fichero no corresponde a ningún formato aceptado",
        )
        db.add(row)
        db.commit()

        response = client.get(f"/ui/documents/{row.id}/original")
        assert response.status_code == 404
        assert "Traceback" not in response.text


class TestTheReviewScreenSpeaksOneLanguage:
    @pytest.fixture
    def client(self, wired_settings: Settings, db: Session) -> Iterator[TestClient]:
        from iep.api.app import create_app

        with TestClient(create_app()) as client:
            yield client

    def test_every_finding_detail_key_has_a_spanish_label(self) -> None:
        """A key with no label renders as its own snake_case name.

        That is how `FOUND` and `EXPECTED` appeared on a screen whose every
        other word was Spanish. These are the keys the rule catalogue actually
        emits, read out of the rules module rather than from a list kept by
        hand somewhere else.
        """
        import re

        from iep.api.vocabulary import _DETAIL_LABELS

        source = (
            Path(__file__).resolve().parents[2] / "src" / "iep" / "validation" / "rules.py"
        ).read_text(encoding="utf-8")
        # Keys as they are written inside a `detail={...}` mapping.
        emitted = set(re.findall(r'"([a-z][a-z0-9_]*)":\s', source))
        # Not every quoted key in the module goes into `detail`; the ones that
        # matter are those a finding carries, so intersect with what the
        # vocabulary is responsible for plus anything new.
        ignored = {"kind", "page", "sheet", "cell", "row", "column"}
        missing = sorted(k for k in emitted - ignored if k not in _DETAIL_LABELS)
        assert missing == [], f"claves de detalle sin etiqueta en español: {missing}"

    def test_a_finding_names_the_document_it_affects(
        self,
        client: TestClient,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """A finding stores its document ids as strings inside JSONB.

        The screen looked them up in a map keyed by UUID, so every lookup
        missed and the "affects" chip printed a raw identifier at a reviewer
        instead of the filename.
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
        db.commit()

        body = client.get(f"/ui/dossiers/{dossier.id}").text
        assert "justificante" in body, "no filename reached the screen"
        affected = [
            str(value)
            for finding in db.execute(
                select(Finding).where(Finding.dossier_id == dossier.id)
            ).scalars()
            for value in (finding.document_ids or [])
        ]
        assert affected, "the corpus should produce findings that name a document"
        for document_id in affected:
            assert f">{document_id}<" not in body, f"raw id {document_id} shown to a reviewer"

    def test_the_reviewer_name_starts_empty(self, client: TestClient, db: Session) -> None:
        """A pre-filled name signs the audit trail with a placeholder."""
        from tests.conftest import new_dossier

        dossier = new_dossier(db)
        db.commit()
        body = client.get(f"/ui/dossiers/{dossier.id}").text
        assert 'id="actor"' in body
        assert 'value="revisor"' not in body
        assert "Nombre del revisor" in body


class TestTheFiledReportIsReadable:
    """The report is what leaves the building, so it is held to the screen's
    standard rather than a looser one: Spanish throughout, every figure with a
    way back to its source, and it has to print.
    """

    def _html(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
    ) -> tuple[str, Dossier]:
        dossier = seed(
            db,
            store,
            settings,
            corpus_dir,
            DOSSIER_B.reference,
            claimed_total=DOSSIER_B.claimed_total_eur,
        )
        run(db, store, settings, dossier)
        return render.render_html(db, dossier.id).html.decode("utf-8"), dossier

    def test_the_reconciliation_table_leads_and_names_both_sides(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        html, _ = self._html(db, store, settings, corpus_dir)
        assert "Lo declarado frente a lo acreditado" in html
        for concept, _, _, _ in vocab.RECONCILIATION:
            assert concept in html, concept
        # The comparison is the point: a table of declared figures alone would
        # not say whether anything was justified.
        assert "Descuadre" in html or "Cuadra" in html

    def test_no_field_path_is_the_only_label_a_reader_gets(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """`report.declared_total_eur` is an identifier, not a heading.

        It still appears - under the Spanish label, so a developer can find the
        field - but never on its own.
        """
        html, dossier = self._html(db, store, settings, corpus_dir)
        paths = [
            row.field_path
            for row in db.execute(
                select(Extraction).where(Extraction.dossier_id == dossier.id)
            ).scalars()
        ]
        assert paths
        for path in set(paths):
            label = vocab.field_label(path)
            assert label != path, f"{path} no tiene etiqueta en español"
            assert label in html, f"falta la etiqueta de {path}"

    def test_no_english_leaks_through_a_finding_detail(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """A detail value that is a key must be translated, not printed.

        `window_source` used to hold the sentence "published call page
        http://..." and the report printed it verbatim, in English, in the
        middle of a Spanish artefact.
        """
        html, _ = self._html(db, store, settings, corpus_dir)
        for leaked in ("published call page", "dossier period (", "CALL_PAGE"):
            assert leaked not in html, leaked
        assert "La convocatoria publicada" in html

    def test_every_figure_offers_its_evidence(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        html, dossier = self._html(db, store, settings, corpus_dir)
        rows = list(
            db.execute(select(Extraction).where(Extraction.dossier_id == dossier.id)).scalars()
        )
        assert rows
        for row in rows:
            assert f'/ui/evidence/{row.id}"' in html, f"{row.field_path} sin enlace"

    def test_a_refused_document_is_named_with_its_reason(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """The report used to list only what was accepted, so a dossier missing
        two receipts read as complete.
        """
        html, dossier = self._html(db, store, settings, corpus_dir)
        refused = [
            row
            for row in db.execute(
                select(Document).where(Document.dossier_id == dossier.id)
            ).scalars()
            if row.status in (DocumentStatus.UNSUPPORTED, DocumentStatus.CORRUPT)
        ]
        assert refused, "el corpus incluye ficheros que se rechazan"
        assert "Documentos que no se aceptaron" in html
        for row in refused:
            assert row.original_filename in html
            if row.rejection_reason:
                assert row.rejection_reason in html

    def test_it_prints_and_carries_the_logo_frame(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """ "Descargar PDF" is the browser's print dialogue, so the print rules
        are the feature: without them the buttons print as dead controls and a
        dark theme comes out of the printer as a black page.
        """
        html, _ = self._html(db, store, settings, corpus_dir)
        assert "logo de la empresa" in html
        assert "@media print" in html
        assert "@page" in html
        assert ".noprint { display: none !important; }" in html
        assert "window.print()" in html

    def test_it_needs_nothing_from_the_network(
        self,
        db: Session,
        store: LocalObjectStore,
        settings: Settings,
        corpus_dir: Path,
        requires_ocr: None,
    ) -> None:
        """A stored report has to render the same years later, from a copy
        nobody kept the application next to.
        """
        html, _ = self._html(db, store, settings, corpus_dir)
        for external in ("<link", "<script src", "@import", "http://fonts", "cdn."):
            assert external not in html, external
