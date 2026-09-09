"""The processing pipeline for one dossier.

Order: capture the external sources, read every document, extract fields with
their locators, index the text for evidence lookup, compute the aggregates the
rules need, run the rules, and hand the result to a human.

Two properties are load-bearing and are asserted by tests:

* it is idempotent. Extractions are upserted by a dedup key derived from the
  document, the field and the locator, chunks are rebuilt per document, and
  findings are upserted by fingerprint. Running it twice on unchanged inputs
  changes no counts;
* it never approves anything. The terminal state of a successful run is
  NEEDS_REVIEW, whether or not there are findings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from iep.audit import service as audit
from iep.config import Settings
from iep.connectors.public_page import (
    SCRAPER_VERSION,
    PageCapture,
    PublicPageScraper,
    ScraperError,
)
from iep.connectors.registry import (
    CONNECTOR_VERSION,
    ConnectorError,
    RegistryConnector,
    RegistryPerson,
    RegistrySnapshot,
)
from iep.db.models import Document, DocumentChunk, Dossier
from iep.db.models import Extraction as ExtractionRow
from iep.domain.contracts import CONTRACT_VERSION, ApiFieldLocator, DerivedLocator
from iep.domain.enums import (
    AuditAction,
    DocumentKind,
    DocumentStatus,
    DossierStatus,
    ExtractionMethod,
    FieldStatus,
    MediaKind,
    SourceKind,
)
from iep.dossiers import service as dossiers
from iep.extraction import fields as field_readers
from iep.extraction import ocr, pdf_text
from iep.extraction.base import ExtractionError, FieldCandidate, TextChunk
from iep.extraction.excel import EXTRACTOR_VERSION as EXCEL_EXTRACTOR_VERSION
from iep.extraction.excel import Sheet, read_workbook
from iep.observability import metrics
from iep.semantic.protocol import (
    SemanticExtractor,
    SemanticFieldSpec,
    SemanticProviderError,
    SemanticRequest,
)
from iep.storage.base import ObjectStore
from iep.validation import engine as validation_engine
from iep.validation.rules import CallWindow, RuleContext, evaluate

log = logging.getLogger(__name__)

PIPELINE_VERSION = "pipeline/1.0.0"

# Documents refused at ingestion. Their bytes were never stored.
_UNREADABLE = frozenset({DocumentStatus.UNSUPPORTED, DocumentStatus.CORRUPT})

_SEMANTIC_FIELDS = (
    SemanticFieldSpec(
        field_path="report.project_code",
        description="The dossier reference the report belongs to.",
        value_type="text",
    ),
    SemanticFieldSpec(
        field_path="report.title", description="The project title.", value_type="text"
    ),
    SemanticFieldSpec(
        field_path="invoice.number", description="The invoice number.", value_type="text"
    ),
    SemanticFieldSpec(
        field_path="invoice.total_eur",
        description="The total amount of the invoice as printed.",
        value_type="number",
    ),
)

_CANDIDATE_KINDS = (
    DocumentKind.TECHNICAL_REPORT,
    DocumentKind.EXPENSE_INVOICE,
    DocumentKind.TIMESHEET,
)


@dataclass
class DocumentOutcome:
    document_id: uuid.UUID
    kind: DocumentKind
    kind_confidence: float
    candidates: list[FieldCandidate] = field(default_factory=list)
    chunks: list[TextChunk] = field(default_factory=list)
    text: str = ""
    ocr_confidence: float | None = None
    used_ocr: bool = False
    stage_seconds: float = 0.0
    error: str | None = None
    semantic_cost_eur: float = 0.0
    semantic_warnings: list[str] = field(default_factory=list)
    # Whichever reader ran keeps its own structured output here so the field
    # readers can work from it without re-parsing the bytes.
    pdf_pages: pdf_text.PdfPages | None = None
    ocr_pages: list[ocr.OcrPage] = field(default_factory=list)
    sheets: list[Sheet] = field(default_factory=list)


@dataclass
class ProcessingResult:
    dossier_id: uuid.UUID
    documents_processed: int
    documents_failed: int
    extractions_written: int
    chunks_written: int
    summary: validation_engine.ValidationSummary
    per_stage_seconds: dict[str, float]
    semantic_provider: str
    semantic_config_hash: str
    estimated_llm_cost_eur: float
    registry_people: int
    call_window: CallWindow
    warnings: list[str] = field(default_factory=list)


def process_dossier(
    session: Session,
    store: ObjectStore,
    settings: Settings,
    *,
    dossier: Dossier,
    semantic: SemanticExtractor,
    registry: RegistryConnector | None = None,
    scraper: PublicPageScraper | None = None,
) -> ProcessingResult:
    started = time.monotonic()
    stages: dict[str, float] = {}
    warnings: list[str] = []
    estimated_cost = 0.0

    stage = time.monotonic()
    registry_people, registry_candidates, registry_document_ids, registry_warning = (
        _capture_registry(session, store, settings, dossier, registry)
    )
    if registry_warning:
        warnings.append(registry_warning)
    call_window, call_candidates, call_document_ids, call_warning = _capture_call_page(
        session, store, settings, dossier, scraper
    )
    if call_warning:
        warnings.append(call_warning)
    stages["external_capture"] = time.monotonic() - stage

    documents = list(
        session.execute(
            select(Document)
            .where(Document.dossier_id == dossier.id)
            .order_by(Document.received_at.asc())
        ).scalars()
    )

    outcomes: list[DocumentOutcome] = []
    for document in documents:
        if document.source_kind is not SourceKind.UPLOAD:
            continue
        if document.status in _UNREADABLE:
            # Refused at ingestion: recorded, never stored, nothing to read.
            # The intake rule reports it to the reviewer.
            continue
        outcome = _process_document(store, settings, document, semantic)
        outcomes.append(outcome)
        if outcome.error:
            document.status = DocumentStatus.FAILED
            document.rejection_reason = outcome.error
        else:
            document.status = DocumentStatus.EXTRACTED
            document.document_kind = outcome.kind
    stages["document_extraction"] = sum(o.stage_seconds for o in outcomes)

    candidates: list[FieldCandidate] = []
    document_of_candidate: list[uuid.UUID | None] = []
    for outcome in outcomes:
        estimated_cost += outcome.semantic_cost_eur
        warnings.extend(outcome.semantic_warnings)
        for candidate in outcome.candidates:
            candidates.append(candidate)
            document_of_candidate.append(outcome.document_id)

    candidates.extend(registry_candidates)
    document_of_candidate.extend(registry_document_ids)
    candidates.extend(call_candidates)
    document_of_candidate.extend(call_document_ids)

    stage = time.monotonic()
    written = _persist_extractions(
        session,
        dossier.id,
        candidates,
        document_of_candidate,
        review_threshold=settings.review_confidence_threshold,
    )
    chunks_written = _persist_chunks(session, dossier.id, outcomes)
    stages["persist"] = time.monotonic() - stage

    session.flush()
    extractions = list(
        session.execute(
            select(ExtractionRow).where(ExtractionRow.dossier_id == dossier.id)
        ).scalars()
    )

    stage = time.monotonic()
    aggregates = _aggregate_candidates(extractions)
    written += _persist_extractions(
        session,
        dossier.id,
        aggregates,
        [None] * len(aggregates),
        review_threshold=settings.review_confidence_threshold,
    )
    session.flush()
    extractions = list(
        session.execute(
            select(ExtractionRow).where(ExtractionRow.dossier_id == dossier.id)
        ).scalars()
    )
    stages["aggregate"] = time.monotonic() - stage

    stage = time.monotonic()
    context = RuleContext(
        dossier=dossier,
        documents=documents,
        extractions=extractions,
        registry={p.employee_id: p for p in registry_people},
        call_window=call_window,
        ocr_confidence_by_document={
            o.document_id: o.ocr_confidence for o in outcomes if o.ocr_confidence is not None
        },
        document_text_by_id={o.document_id: o.text for o in outcomes if o.text},
        registry_available=registry_warning is None,
        external_capture_errors=tuple(
            (source, warning)
            for source, warning in (
                ("personnel registry", registry_warning),
                ("published call page", call_warning),
            )
            if warning is not None
        ),
        formula_cells_by_document={
            outcome.document_id: tuple(
                formula for sheet in outcome.sheets for formula in sheet.formula_cells
            )
            for outcome in outcomes
            if outcome.sheets and any(sheet.formula_cells for sheet in outcome.sheets)
        },
    )
    summary = validation_engine.persist(session, dossier.id, evaluate(context))
    stages["validation"] = time.monotonic() - stage
    stages["total"] = time.monotonic() - started

    metrics.observe_duration("iep_pipeline_seconds", stages["total"])
    metrics.increment("iep_dossiers_processed_total")

    audit.record(
        session,
        action=AuditAction.PROCESSING_FINISHED,
        dossier_id=dossier.id,
        payload={
            "pipeline_version": PIPELINE_VERSION,
            "documents": len(outcomes),
            "extractions": written,
            "findings_open": summary.open_total,
            "findings_created": summary.created,
            "semantic_provider": semantic.name,
            "semantic_config_hash": semantic.config_hash(),
            "estimated_llm_cost_eur": round(estimated_cost, 6),
            "stages_seconds": {k: round(v, 3) for k, v in stages.items()},
        },
    )

    return ProcessingResult(
        dossier_id=dossier.id,
        documents_processed=sum(1 for o in outcomes if not o.error),
        documents_failed=sum(1 for o in outcomes if o.error),
        extractions_written=written,
        chunks_written=chunks_written,
        summary=summary,
        per_stage_seconds={k: round(v, 4) for k, v in stages.items()},
        semantic_provider=semantic.name,
        semantic_config_hash=semantic.config_hash(),
        estimated_llm_cost_eur=estimated_cost,
        registry_people=len(registry_people),
        call_window=call_window,
        warnings=warnings,
    )


def finalise_state(session: Session, dossier: Dossier) -> DossierStatus:
    """A successful run always ends in NEEDS_REVIEW. Approval is a human act."""
    dossiers.try_transition(
        session, dossier, DossierStatus.NEEDS_REVIEW, reason="processing completed"
    )
    return DossierStatus(dossier.status)


# --------------------------------------------------------------------------
# Per-document reading
# --------------------------------------------------------------------------


def _process_document(
    store: ObjectStore,
    settings: Settings,
    document: Document,
    semantic: SemanticExtractor,
) -> DocumentOutcome:
    started = time.monotonic()
    outcome = DocumentOutcome(
        document_id=document.id, kind=DocumentKind.UNKNOWN, kind_confidence=0.0
    )
    try:
        data = store.get(document.storage_key)
        if document.media_kind is MediaKind.PDF:
            _read_pdf(settings, data, outcome)
        elif document.media_kind in (MediaKind.PNG, MediaKind.JPEG):
            _read_image(settings, data, outcome)
        elif document.media_kind is MediaKind.XLSX:
            _read_workbook(settings, data, outcome)
        else:
            outcome.error = f"no reader for {document.media_kind}"
            return outcome
    except ExtractionError as exc:
        outcome.error = str(exc)
        metrics.increment("iep_extraction_failures_total", media=str(document.media_kind))
        outcome.stage_seconds = time.monotonic() - started
        return outcome

    kind, confidence = _classify(outcome, semantic, document)
    outcome.kind = kind
    outcome.kind_confidence = confidence
    outcome.candidates = _fields_for_kind(kind, outcome)
    outcome.stage_seconds = time.monotonic() - started
    return outcome


def _read_pdf(settings: Settings, data: bytes, outcome: DocumentOutcome) -> None:
    pages = pdf_text.read_pages(data)
    if pages.has_usable_text_layer():
        outcome.text = pages.text
        outcome.chunks = list(pdf_text.chunk_pages(pages))
        outcome.used_ocr = False
        outcome.pdf_pages = pages
        return

    # No usable text layer: this is a scan wearing a PDF wrapper.
    ocr_pages = [
        ocr.recognise(
            pdf_text.render_page_png(
                data,
                page,
                dpi=settings.ocr_dpi,
                max_pixels=settings.max_image_pixels,
            ),
            page=page,
            language=settings.ocr_language,
            timeout_seconds=settings.ocr_timeout_seconds,
        )
        for page in range(1, min(pdf_text.page_count(data), settings.max_pdf_pages) + 1)
    ]
    outcome.text = "\n".join(page.text for page in ocr_pages)
    outcome.chunks = list(ocr.chunks_from_pages(ocr_pages))
    outcome.ocr_confidence = ocr.mean_confidence(ocr_pages)
    outcome.used_ocr = True
    outcome.ocr_pages = ocr_pages


def _read_image(settings: Settings, data: bytes, outcome: DocumentOutcome) -> None:
    page = ocr.recognise(
        data,
        page=1,
        language=settings.ocr_language,
        timeout_seconds=settings.ocr_timeout_seconds,
    )
    outcome.text = page.text
    outcome.chunks = list(ocr.chunks_from_pages([page]))
    outcome.ocr_confidence = ocr.mean_confidence([page])
    outcome.used_ocr = True
    outcome.ocr_pages = [page]


def _read_workbook(settings: Settings, data: bytes, outcome: DocumentOutcome) -> None:
    sheets = read_workbook(data, max_cells=settings.max_excel_cells)
    outcome.text = "\n".join(
        " ".join(cell.text for cell in row if cell.text) for sheet in sheets for row in sheet.rows
    )
    outcome.sheets = sheets
    outcome.used_ocr = False


def _classify(
    outcome: DocumentOutcome, semantic: SemanticExtractor, document: Document
) -> tuple[DocumentKind, float]:
    """Ask the semantic provider what this document is.

    Only the classification is used. The provider also returns grounded field
    proposals, and they are deliberately not persisted as extractions: every
    value a rule compares comes from a deterministic reader with a locator. The
    proposals are useful for a reviewer and for measuring the provider, not for
    deciding an amount - which is why a document that tries to instruct the
    model cannot move a number.
    """
    try:
        result = semantic.run(
            SemanticRequest(
                document_id=str(document.id),
                text=outcome.text,
                fields=_SEMANTIC_FIELDS,
                candidate_kinds=_CANDIDATE_KINDS,
            )
        )
    except SemanticProviderError as exc:
        # Classification failing is not fatal: the document still has a media
        # kind and the reader can fall back on it.
        log.warning("semantic_provider_failed", extra={"error": str(exc)})
        metrics.increment("iep_semantic_failures_total", provider=semantic.name)
        return _fallback_kind(document), 0.0
    outcome.semantic_cost_eur = result.usage.estimated_cost_eur
    outcome.semantic_warnings.extend(
        f"{document.original_filename}: {warning}" for warning in result.warnings
    )
    return result.result.document_kind, result.result.kind_confidence


def _fallback_kind(document: Document) -> DocumentKind:
    if document.media_kind is MediaKind.XLSX:
        return DocumentKind.TIMESHEET
    if document.media_kind in (MediaKind.PNG, MediaKind.JPEG):
        return DocumentKind.EXPENSE_INVOICE
    return DocumentKind.UNKNOWN


def _fields_for_kind(kind: DocumentKind, outcome: DocumentOutcome) -> list[FieldCandidate]:
    if kind is DocumentKind.TECHNICAL_REPORT and outcome.pdf_pages is not None:
        found = field_readers.report_fields(
            outcome.pdf_pages, extractor_version=pdf_text.EXTRACTOR_VERSION
        )
        return found + _expand_period(found, pdf_text.EXTRACTOR_VERSION)
    if kind is DocumentKind.EXPENSE_INVOICE and outcome.ocr_pages:
        return field_readers.invoice_fields(
            outcome.ocr_pages, extractor_version=ocr.EXTRACTOR_VERSION
        )
    if kind is DocumentKind.TIMESHEET and outcome.sheets:
        return field_readers.timesheet_fields(
            outcome.sheets, extractor_version=EXCEL_EXTRACTOR_VERSION
        )
    return []


def _expand_period(found: list[FieldCandidate], extractor_version: str) -> list[FieldCandidate]:
    """`report.period` is one printed line holding two dates."""
    period = next((c for c in found if c.field_path == "report.period"), None)
    if period is None or not period.value_text:
        return []
    start, end = field_readers.split_period(period.value_text)
    out: list[FieldCandidate] = []
    for suffix, value in (("period_start", start), ("period_end", end)):
        if value is None:
            continue
        out.append(
            FieldCandidate(
                field_path=f"report.{suffix}",
                locator=period.locator,
                method=ExtractionMethod.PDF_TEXT,
                extractor_version=extractor_version,
                confidence=period.confidence,
                value_text=value.isoformat(),
                value_date=value,
            )
        )
    return out


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def dedup_key(document_id: uuid.UUID | None, candidate: FieldCandidate) -> str:
    """Stable across reruns: same document, same field, same place, same key.

    A derived value keys on the rule that produced it, not on the ids of the
    extractions it summed. Those ids are evidence and stay in the locator, but
    including them in the key made the key depend on row ordering, so a replay
    inserted a second copy of every aggregate instead of updating the first.
    """
    locator = candidate.locator.model_dump(mode="json")
    if locator.get("kind") == "DERIVED":
        locator = {"kind": "DERIVED", "rule": locator.get("rule")}
    raw = json.dumps(
        {
            "document_id": str(document_id) if document_id else None,
            "field_path": candidate.field_path,
            "locator": locator,
            "method": str(candidate.method),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _persist_extractions(
    session: Session,
    dossier_id: uuid.UUID,
    candidates: list[FieldCandidate],
    document_ids: list[uuid.UUID | None],
    *,
    review_threshold: float,
) -> int:
    written = 0
    for candidate, document_id in zip(candidates, document_ids, strict=True):
        key = dedup_key(document_id, candidate)
        status = (
            FieldStatus.NEEDS_REVIEW
            if candidate.confidence < review_threshold
            else FieldStatus.EXTRACTED
        )
        values = {
            "id": uuid.uuid4(),
            "dossier_id": dossier_id,
            "document_id": document_id,
            "field_path": candidate.field_path,
            "value_text": candidate.value_text,
            "value_number": candidate.value_number,
            "value_date": candidate.value_date,
            "locator": candidate.locator.model_dump(mode="json"),
            "method": str(candidate.method),
            "extractor_version": candidate.extractor_version,
            "contract_version": CONTRACT_VERSION,
            "confidence": candidate.confidence,
            "status": status,
            "dedup_key": key,
        }
        stmt = (
            pg_insert(ExtractionRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["dossier_id", "dedup_key"],
                # A human correction is never overwritten by a re-run.
                set_={
                    "value_text": values["value_text"],
                    "value_number": values["value_number"],
                    "value_date": values["value_date"],
                    "confidence": values["confidence"],
                    "extractor_version": values["extractor_version"],
                    "contract_version": values["contract_version"],
                    "locator": values["locator"],
                    "status": values["status"],
                },
                where=ExtractionRow.status.not_in((FieldStatus.CORRECTED, FieldStatus.CONFIRMED)),
            )
        )
        session.execute(stmt)
        written += 1
    return written


def _persist_chunks(
    session: Session, dossier_id: uuid.UUID, outcomes: list[DocumentOutcome]
) -> int:
    total = 0
    for outcome in outcomes:
        active_ordinals: list[int] = []
        for chunk in outcome.chunks:
            active_ordinals.append(chunk.ordinal)
            session.execute(
                pg_insert(DocumentChunk)
                .values(
                    id=uuid.uuid4(),
                    dossier_id=dossier_id,
                    document_id=outcome.document_id,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    locator=chunk.locator.model_dump(mode="json"),
                )
                .on_conflict_do_update(
                    index_elements=["document_id", "ordinal"],
                    set_={
                        "text": chunk.text,
                        "locator": chunk.locator.model_dump(mode="json"),
                    },
                )
            )
            total += 1
        stale = delete(DocumentChunk).where(DocumentChunk.document_id == outcome.document_id)
        if active_ordinals:
            stale = stale.where(DocumentChunk.ordinal.not_in(active_ordinals))
        session.execute(stale)
    return total


def _aggregate_candidates(extractions: list[ExtractionRow]) -> list[FieldCandidate]:
    # Sorted so the recorded evidence trail is stable between runs even
    # though the database returns rows in whatever order it likes.
    ordered = sorted(extractions, key=lambda row: (row.field_path, str(row.id)))
    invoice_totals = [
        (e, e.value_number)
        for e in ordered
        if e.field_path == "invoice.total_eur"
        and e.value_number is not None
        and e.status != FieldStatus.REJECTED
    ]
    timesheet_amounts = [
        (e, e.value_number)
        for e in ordered
        if e.field_path.startswith("timesheet.rows[")
        and e.field_path.endswith(".amount_eur")
        and e.value_number is not None
        and e.status != FieldStatus.REJECTED
    ]

    out: list[FieldCandidate] = []
    if invoice_totals:
        out.append(
            _derived(
                "invoices.total_eur",
                sum((amount for _, amount in invoice_totals), Decimal("0.00")),
                tuple(e.id for e, _ in invoice_totals),
                "sum of invoice.total_eur across expense receipts",
                min(float(e.confidence) for e, _ in invoice_totals),
            )
        )
        out.append(
            _derived(
                "invoices.count",
                Decimal(len(invoice_totals)),
                tuple(e.id for e, _ in invoice_totals),
                "count of expense receipts with a readable total",
                1.0,
            )
        )
    if timesheet_amounts:
        out.append(
            _derived(
                "timesheet.total_amount_eur",
                sum((amount for _, amount in timesheet_amounts), Decimal("0.00")),
                tuple(e.id for e, _ in timesheet_amounts),
                "sum of timesheet row amounts",
                min(float(e.confidence) for e, _ in timesheet_amounts),
            )
        )
        out.append(
            _derived(
                "timesheet.row_count",
                Decimal(len(timesheet_amounts)),
                tuple(e.id for e, _ in timesheet_amounts),
                "count of timesheet rows with a readable amount",
                1.0,
            )
        )
    return out


def _derived(
    field_path: str,
    total: Decimal,
    inputs: tuple[uuid.UUID, ...],
    rule: str,
    confidence: float,
) -> FieldCandidate:
    return FieldCandidate(
        field_path=field_path,
        locator=DerivedLocator(inputs=inputs, rule=rule),
        method=ExtractionMethod.AGGREGATED,
        extractor_version=PIPELINE_VERSION,
        confidence=round(confidence, 3),
        value_number=total,
        value_text=str(total),
    )


def _capture_registry(
    session: Session,
    store: ObjectStore,
    settings: Settings,
    dossier: Dossier,
    connector: RegistryConnector | None,
) -> tuple[list[RegistryPerson], list[FieldCandidate], list[uuid.UUID | None], str | None]:
    owns_connector = connector is None
    connector = connector or RegistryConnector(settings)
    try:
        snapshot = connector.fetch_personnel()
    except ConnectorError as exc:
        log.warning("registry_capture_failed", extra={"error": str(exc)})
        metrics.increment("iep_external_capture_failures_total", source="registry")
        return [], [], [], f"personnel registry unavailable: {exc}"
    finally:
        if owns_connector:
            connector.close()

    document = _store_capture(
        session,
        store,
        dossier,
        data=snapshot.raw_payload,
        filename="registry-personnel.json",
        media_kind=MediaKind.JSON,
        source_kind=SourceKind.REGISTRY_API,
        source_detail=f"{snapshot.endpoint} ({snapshot.contract_version})",
    )
    candidates = _registry_candidates(snapshot)
    return list(snapshot.people), candidates, [document.id] * len(candidates), None


def _capture_call_page(
    session: Session,
    store: ObjectStore,
    settings: Settings,
    dossier: Dossier,
    scraper: PublicPageScraper | None,
) -> tuple[CallWindow, list[FieldCandidate], list[uuid.UUID | None], str | None]:
    if not dossier.call_page_url:
        return CallWindow(None, None, "dossier period (no call page recorded)"), [], [], None

    owns_scraper = scraper is None
    scraper = scraper or PublicPageScraper(settings)
    try:
        capture = scraper.capture(dossier.call_page_url)
    except ScraperError as exc:
        log.warning("call_page_capture_failed", extra={"error": str(exc)})
        metrics.increment("iep_external_capture_failures_total", source="call_page")
        return (
            CallWindow(None, None, "dossier period (call page unavailable)"),
            [],
            [],
            (f"published call page unavailable: {exc}"),
        )
    finally:
        if owns_scraper:
            scraper.close()

    document = _store_capture(
        session,
        store,
        dossier,
        data=capture.html,
        filename="call-page.html",
        media_kind=MediaKind.HTML,
        source_kind=SourceKind.PUBLIC_PAGE,
        source_detail=capture.url,
    )
    candidates = _call_page_candidates(capture, captured_at=document.received_at)
    return (
        CallWindow(
            capture.as_date("call.eligible_from"),
            capture.as_date("call.eligible_to"),
            f"published call page {capture.url}",
            max_funding_eur=capture.as_amount("call.max_funding_eur"),
        ),
        candidates,
        [document.id] * len(candidates),
        None,
    )


def _store_capture(
    session: Session,
    store: ObjectStore,
    dossier: Dossier,
    *,
    data: bytes,
    filename: str,
    media_kind: MediaKind,
    source_kind: SourceKind,
    source_detail: str,
) -> Document:
    """Persist an external capture as a first-class document.

    A captured payload is evidence like any other: it gets a hash, a stored
    object and a provenance row, so a figure taken from the registry is as
    traceable as one read off a page.
    """
    digest = hashlib.sha256(data).hexdigest()
    existing = session.execute(
        select(Document).where(Document.dossier_id == dossier.id, Document.content_sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    key = store.put(digest, data)
    document = Document(
        id=uuid.uuid4(),
        dossier_id=dossier.id,
        original_filename=filename,
        declared_media_type=None,
        media_kind=media_kind,
        document_kind=DocumentKind.UNKNOWN,
        status=DocumentStatus.EXTRACTED,
        source_kind=source_kind,
        source_detail=source_detail[:1000],
        size_bytes=len(data),
        content_sha256=digest,
        storage_key=key,
        page_count=None,
    )
    session.add(document)
    session.flush()
    audit.record(
        session,
        action=AuditAction.DOCUMENT_RECEIVED,
        dossier_id=dossier.id,
        payload={
            "filename": filename,
            "document_id": str(document.id),
            "source_kind": str(source_kind),
            "source_detail": source_detail[:200],
            "content_sha256": digest,
            "size_bytes": len(data),
        },
    )
    return document


def _registry_candidates(snapshot: RegistrySnapshot) -> list[FieldCandidate]:
    candidates: list[FieldCandidate] = []
    for person in snapshot.people:
        prefix = f"registry.personnel[{person.employee_id}]"
        values: tuple[tuple[str, object], ...] = (
            ("employee_id", person.employee_id),
            ("full_name", person.full_name),
            ("role", person.role),
            ("hourly_rate_eur", person.hourly_rate_eur),
            ("contract_start", person.contract_start),
            ("contract_end", person.contract_end),
        )
        for suffix, value in values:
            if value is None:
                continue
            candidates.append(
                FieldCandidate(
                    field_path=f"{prefix}.{suffix}",
                    locator=ApiFieldLocator(
                        endpoint=snapshot.endpoint,
                        record_id=person.employee_id,
                        json_path=f"$.pages[*].items[employee_id={person.employee_id}].{suffix}",
                        contract_version=snapshot.contract_version,
                    ),
                    method=ExtractionMethod.HTTP_API,
                    extractor_version=CONNECTOR_VERSION,
                    confidence=1.0,
                    value_text=str(value),
                    value_number=value if isinstance(value, Decimal) else None,
                    value_date=value if isinstance(value, date) else None,
                )
            )
    return candidates


def _call_page_candidates(capture: PageCapture, *, captured_at: datetime) -> list[FieldCandidate]:
    candidates: list[FieldCandidate] = []
    for captured in capture.fields:
        value_date = (
            capture.as_date(captured.field_path)
            if captured.field_path.endswith(("from", "to"))
            else None
        )
        value_number = (
            capture.as_amount(captured.field_path)
            if captured.field_path == "call.max_funding_eur"
            else None
        )
        candidates.append(
            FieldCandidate(
                field_path=captured.field_path,
                locator=captured.locator.model_copy(update={"captured_at": captured_at}),
                method=ExtractionMethod.HTML_SELECTOR,
                extractor_version=SCRAPER_VERSION,
                confidence=1.0,
                value_text=captured.value_text,
                value_number=value_number,
                value_date=value_date,
            )
        )
    return candidates
