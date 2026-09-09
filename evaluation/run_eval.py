"""Measure the pipeline against the corpus ground truth.

    python -m evaluation.run_eval --corpus corpus/out --out evaluation/out

Everything reported here is produced by running the pipeline, not asserted by
hand. The harness rebuilds the dossiers from scratch, times each stage, compares
every extracted field against the ground truth the generator wrote, checks which
seeded defects were caught, replays the pipeline to confirm the run is a no-op,
and writes both a JSON record and a readable summary.

Two things it deliberately does not do: call a billable model, and report a
saving. Cost and saving appear only as parameterised scenarios, because the
inputs that would make them real are not available here.
"""

from __future__ import annotations

import argparse
import json
import time
import tracemalloc
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from corpus.dataset import DOSSIERS
from sqlalchemy import select

from iep.config import get_settings
from iep.db.models import Document, DocumentChunk, Extraction, Finding
from iep.db.session import session_scope
from iep.domain.contracts import DossierCreate
from iep.domain.enums import DossierStatus
from iep.dossiers import service as dossiers
from iep.ingestion.service import IngestionRejectedError, ingest_upload
from iep.pipeline.processor import finalise_state, process_dossier
from iep.retrieval import search as retrieval
from iep.semantic.llm import estimate_cost_eur
from iep.storage.local import LocalObjectStore
from iep.worker.runner import build_semantic_provider

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}

# Fields checked against ground truth. Amounts are compared numerically so a
# formatting difference is not counted as an extraction error.
NUMERIC_FIELDS = {
    "report.declared_personnel_cost_eur",
    "report.declared_external_cost_eur",
    "report.declared_total_eur",
    "timesheet.total_amount_eur",
    "timesheet.row_count",
    "invoices.total_eur",
    "invoices.count",
}

RETRIEVAL_PROBES = (
    ("periodo de ejecucion", "report"),
    ("coste de personal declarado", "report"),
    ("base imponible", "invoice"),
)


@dataclass
class FieldResult:
    field_path: str
    expected: str
    actual: str | None
    correct: bool


@dataclass
class InvoiceResult:
    invoice_number: str
    hard_to_read: bool
    fields_expected: int
    fields_correct: int
    missing_fields: list[str] = field(default_factory=list)
    wrong_fields: list[str] = field(default_factory=list)


@dataclass
class DossierResult:
    reference: str
    documents_submitted: int
    documents_accepted: int
    documents_refused: int
    documents_processed: int
    extractions: int
    chunks: int
    input_bytes: int
    report_bytes: int
    export_bytes: int
    stage_seconds: dict[str, float]
    peak_memory_mb: float
    fields: list[FieldResult]
    field_accuracy: float
    invoices: list[InvoiceResult]
    ocr_fields_expected: int
    ocr_fields_correct: int
    expected_findings: list[str]
    detected_findings: list[str]
    missed_findings: list[str]
    unexpected_findings: list[str]
    retrieval: list[dict[str, Any]]
    replay_stable: bool
    replay_seconds: float
    needs_review_fields: int
    final_status: str
    semantic_provider: str
    semantic_config_hash: str
    llm_tokens_estimated: int
    llm_cost_estimate_eur: float


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def _matches(field_path: str, expected: str, actual: Extraction | None) -> bool:
    if actual is None:
        return False
    if field_path in NUMERIC_FIELDS:
        left, right = _decimal(expected), actual.value_number
        return left is not None and right is not None and left == right
    if actual.value_date is not None:
        return actual.value_date.isoformat() == expected
    return (actual.value_text or "").strip() == expected.strip()


def evaluate_dossier(
    corpus_dir: Path, reference: str, *, provider_name: str, call_page_url: str
) -> DossierResult:
    settings = get_settings()
    if provider_name:
        settings = settings.model_copy(update={"semantic_provider": provider_name})
    store = LocalObjectStore(settings.storage_root)
    semantic = build_semantic_provider(settings)

    truth = json.loads((corpus_dir / reference / "ground_truth.json").read_text(encoding="utf-8"))
    files = [
        path
        for path in sorted((corpus_dir / reference).iterdir())
        if path.is_file() and path.name != "ground_truth.json"
    ]
    input_bytes = sum(path.stat().st_size for path in files)

    tracemalloc.start()
    with session_scope() as session:
        # A fresh dossier per run: measuring a re-run of an existing one would
        # measure the idempotent path, which is a separate number below.
        unique = f"{reference}"
        dossier = dossiers.get_by_reference(session, unique)
        if dossier is not None:
            session.delete(dossier)
            session.flush()
        dossier = dossiers.create(
            session,
            DossierCreate(
                reference=unique,
                title=truth["title"],
                period_start=truth["period_start"],
                period_end=truth["period_end"],
                claimed_total_eur=Decimal(truth["claimed_total_eur"]),
                call_page_url=call_page_url,
            ),
        )

        accepted = refused = 0
        ingest_started = time.monotonic()
        for path in files:
            try:
                result = ingest_upload(
                    session,
                    store,
                    settings,
                    dossier=dossier,
                    filename=path.name,
                    declared_media_type=MEDIA_TYPES.get(path.suffix.lower()),
                    data=path.read_bytes(),
                )
            except IngestionRejectedError:
                refused += 1
                continue
            if not result.is_duplicate:
                accepted += 1
        ingest_seconds = time.monotonic() - ingest_started

        dossiers.transition(session, dossier, DossierStatus.QUEUED)
        dossiers.transition(session, dossier, DossierStatus.PROCESSING)
        outcome = process_dossier(session, store, settings, dossier=dossier, semantic=semantic)
        finalise_state(session, dossier)
        dossier_id = dossier.id

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    with session_scope() as session:
        extractions = {
            row.field_path: row
            for row in session.execute(
                select(Extraction).where(Extraction.dossier_id == dossier_id)
            ).scalars()
        }
        all_extractions = list(
            session.execute(select(Extraction).where(Extraction.dossier_id == dossier_id)).scalars()
        )
        documents = list(
            session.execute(select(Document).where(Document.dossier_id == dossier_id)).scalars()
        )
        findings = list(
            session.execute(select(Finding).where(Finding.dossier_id == dossier_id)).scalars()
        )
        chunk_count = len(
            list(
                session.execute(
                    select(DocumentChunk).where(DocumentChunk.dossier_id == dossier_id)
                ).scalars()
            )
        )

        field_results = [
            FieldResult(
                field_path=path,
                expected=expected,
                actual=_display(extractions.get(path)),
                correct=_matches(path, expected, extractions.get(path)),
            )
            for path, expected in sorted(truth["fields"].items())
        ]

        document_names = {document.id: document.original_filename for document in documents}
        invoice_results, ocr_expected, ocr_correct = _score_invoices(
            truth, all_extractions, document_names
        )

        retrieval_results = []
        for query, _ in RETRIEVAL_PROBES:
            hits = retrieval.search(session, dossier_id, query, limit=3)
            repeat = retrieval.search(session, dossier_id, query, limit=3)
            retrieval_results.append(
                {
                    "query": query,
                    "hits": len(hits),
                    "top_document": hits[0].document_name if hits else None,
                    "top_locator_kind": hits[0].locator.get("kind") if hits else None,
                    "deterministic": [h.chunk_id for h in hits] == [h.chunk_id for h in repeat],
                }
            )

        from iep.reporting import render

        rendered = render.render_html(session, dossier_id)
        export = render.export_json(session, dossier_id)
        needs_review = sum(1 for row in all_extractions if row.status == "NEEDS_REVIEW")

    # Replay: the same inputs again must change nothing.
    replay_started = time.monotonic()
    with session_scope() as session:
        dossier = dossiers.get(session, dossier_id)
        dossiers.transition(session, dossier, DossierStatus.QUEUED)
        dossiers.transition(session, dossier, DossierStatus.PROCESSING)
        process_dossier(session, store, settings, dossier=dossier, semantic=semantic)
        finalise_state(session, dossier)
    replay_seconds = time.monotonic() - replay_started

    with session_scope() as session:
        after = len(
            list(
                session.execute(
                    select(Extraction).where(Extraction.dossier_id == dossier_id)
                ).scalars()
            )
        )
        findings_after = len(
            list(session.execute(select(Finding).where(Finding.dossier_id == dossier_id)).scalars())
        )
        final_status = str(dossiers.get(session, dossier_id).status)

    detected = sorted({f.rule_id for f in findings})
    expected_findings = sorted(truth["expected_findings"])
    correct_fields = sum(1 for result in field_results if result.correct)

    stage_seconds = dict(outcome.per_stage_seconds)
    stage_seconds["ingestion"] = round(ingest_seconds, 4)

    return DossierResult(
        reference=reference,
        documents_submitted=len(files),
        documents_accepted=accepted,
        documents_refused=refused,
        documents_processed=outcome.documents_processed,
        extractions=len(all_extractions),
        chunks=chunk_count,
        input_bytes=input_bytes,
        report_bytes=len(rendered.html),
        export_bytes=len(export),
        stage_seconds=stage_seconds,
        peak_memory_mb=round(peak / (1024 * 1024), 2),
        fields=field_results,
        field_accuracy=round(correct_fields / len(field_results), 4) if field_results else 0.0,
        invoices=invoice_results,
        ocr_fields_expected=ocr_expected,
        ocr_fields_correct=ocr_correct,
        expected_findings=expected_findings,
        detected_findings=detected,
        missed_findings=sorted(set(expected_findings) - set(detected)),
        unexpected_findings=sorted(set(detected) - set(expected_findings)),
        retrieval=retrieval_results,
        replay_stable=(after == len(all_extractions) and findings_after == len(findings)),
        replay_seconds=round(replay_seconds, 3),
        needs_review_fields=needs_review,
        final_status=final_status,
        semantic_provider=outcome.semantic_provider,
        semantic_config_hash=outcome.semantic_config_hash,
        llm_tokens_estimated=_estimated_tokens(documents),
        llm_cost_estimate_eur=float(
            estimate_cost_eur(get_settings().llm_model, _estimated_tokens(documents), 800)
        ),
    )


def _display(extraction: Extraction | None) -> str | None:
    if extraction is None:
        return None
    if extraction.value_number is not None:
        return str(extraction.value_number)
    if extraction.value_date is not None:
        return extraction.value_date.isoformat()
    return extraction.value_text


def _score_invoices(
    truth: dict[str, Any], extractions: list[Extraction], document_names: dict[Any, str]
) -> tuple[list[InvoiceResult], int, int]:
    """How much of each scanned receipt was read correctly.

    Grouped per document so a receipt that OCR read badly is visible as a
    receipt, not averaged away across the dossier.
    """
    by_document: dict[Any, dict[str, Extraction]] = {}
    for row in extractions:
        if not row.field_path.startswith("invoice."):
            continue
        by_document.setdefault(row.document_id, {})[row.field_path] = row

    results: list[InvoiceResult] = []
    total_expected = total_correct = 0

    for invoice in truth["invoices"]:
        checks = {
            "invoice.number": invoice["invoice_number"],
            "invoice.issue_date": invoice["issue_date"],
            "invoice.supplier_name": invoice["supplier_name"],
            "invoice.base_eur": invoice["base_eur"],
            "invoice.vat_eur": invoice["vat_eur"],
            "invoice.total_eur": invoice["total_eur"],
        }
        # Match the document whose invoice number matches; fall back to the
        # first unmatched one so a badly-read receipt is still scored.
        fields = _find_invoice_document(by_document, invoice["invoice_number"], document_names)
        missing: list[str] = []
        wrong: list[str] = []
        correct = 0
        for path, expected in checks.items():
            row = fields.get(path) if fields else None
            if row is None:
                missing.append(path)
                continue
            if _invoice_field_matches(path, expected, row):
                correct += 1
            else:
                wrong.append(path)
        total_expected += len(checks)
        total_correct += correct
        results.append(
            InvoiceResult(
                invoice_number=invoice["invoice_number"],
                hard_to_read=bool(invoice["hard_to_read"]),
                fields_expected=len(checks),
                fields_correct=correct,
                missing_fields=missing,
                wrong_fields=wrong,
            )
        )
    return results, total_expected, total_correct


def _find_invoice_document(
    by_document: dict[Any, dict[str, Extraction]],
    number: str,
    document_names: dict[Any, str],
) -> dict[str, Extraction] | None:
    """Locate the extractions belonging to one receipt.

    Matching on the extracted number first is the honest path. Falling back to
    the filename matters for the badly-scanned receipt: without it a document
    whose number OCR misread would score zero on every field, which would
    overstate how badly the scan was read.
    """
    for fields in by_document.values():
        row = fields.get("invoice.number")
        if row is not None and (row.value_text or "").strip().upper() == number.upper():
            return fields
    for document_id, fields in by_document.items():
        if number.upper() in document_names.get(document_id, "").upper():
            return fields
    return None


def _invoice_field_matches(path: str, expected: str, row: Extraction) -> bool:
    if path.endswith("_eur"):
        return row.value_number is not None and row.value_number == Decimal(expected)
    if path == "invoice.issue_date":
        return row.value_date is not None and row.value_date.isoformat() == expected
    return (row.value_text or "").strip() == expected.strip()


def _estimated_tokens(documents: list[Document]) -> int:
    """A rough input-token count for the hosted-provider cost scenario.

    Four characters per token is a working approximation, and it is only ever
    used for an estimate that is labelled as one.
    """
    characters = sum(document.size_bytes for document in documents if document.page_count)
    return max(1, characters // 4)


def savings_scenarios() -> list[dict[str, Any]]:
    """A calculator, not a claim.

    Every input is a parameter. Nothing here is measured from any real
    organisation, and the output is a range across assumptions rather than a
    figure. See docs/business-impact.md.
    """
    scenarios = []
    for dossiers_per_year, manual_minutes, hourly_cost, automated_share in (
        (500, 90, 45.0, 0.4),
        (2_000, 90, 45.0, 0.4),
        (2_000, 150, 55.0, 0.6),
        (10_000, 120, 50.0, 0.5),
    ):
        manual_hours = dossiers_per_year * manual_minutes / 60
        gross = manual_hours * hourly_cost * automated_share
        scenarios.append(
            {
                "dossiers_per_year": dossiers_per_year,
                "manual_minutes_per_dossier": manual_minutes,
                "reviewer_cost_eur_per_hour": hourly_cost,
                "share_of_time_removed": automated_share,
                "reviewer_hours_removed_per_year": round(manual_hours * automated_share, 1),
                "gross_reduction_eur_per_year": round(gross, 2),
                "note": "Gross of running cost and of the review time the pipeline itself creates.",
            }
        )
    return scenarios


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("corpus/out"))
    parser.add_argument("--out", type=Path, default=Path("evaluation/out"))
    parser.add_argument("--provider", choices=("deterministic", "llm"), default="deterministic")
    parser.add_argument(
        "--call-page-url",
        default="http://devsources:8080/public/convocatoria.html",
        help="published call page to capture; must be in the scraper allowlist",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = [
        evaluate_dossier(
            args.corpus,
            spec.reference,
            provider_name=args.provider,
            call_page_url=args.call_page_url,
        )
        for spec in DOSSIERS
    ]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": uuid.uuid4().hex[:12],
        "total_seconds": round(time.monotonic() - started, 3),
        "semantic_provider": args.provider,
        "dossiers": [asdict(result) for result in results],
        "totals": _totals(results),
        "savings_scenarios": savings_scenarios(),
    }
    (args.out / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    summary = render_summary(payload)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    return 0


def _totals(results: list[DossierResult]) -> dict[str, Any]:
    fields_checked = sum(len(result.fields) for result in results)
    fields_correct = sum(sum(1 for f in result.fields if f.correct) for result in results)
    ocr_expected = sum(result.ocr_fields_expected for result in results)
    ocr_correct = sum(result.ocr_fields_correct for result in results)
    expected_findings = sum(len(result.expected_findings) for result in results)
    missed = sum(len(result.missed_findings) for result in results)
    unexpected = sum(len(result.unexpected_findings) for result in results)
    return {
        "fields_checked": fields_checked,
        "fields_correct": fields_correct,
        "field_accuracy": round(fields_correct / fields_checked, 4) if fields_checked else 0.0,
        "ocr_fields_checked": ocr_expected,
        "ocr_fields_correct": ocr_correct,
        "ocr_field_accuracy": round(ocr_correct / ocr_expected, 4) if ocr_expected else 0.0,
        "expected_findings": expected_findings,
        "findings_missed": missed,
        "findings_unexpected": unexpected,
        "recall": round((expected_findings - missed) / expected_findings, 4)
        if expected_findings
        else 1.0,
        "input_bytes": sum(result.input_bytes for result in results),
        "documents_processed": sum(result.documents_processed for result in results),
        "replay_stable": all(result.replay_stable for result in results),
    }


def render_summary(payload: dict[str, Any]) -> str:
    totals = payload["totals"]
    lines = [
        "# Measured results",
        "",
        f"Run {payload['run_id']} at {payload['generated_at']} "
        f"using the `{payload['semantic_provider']}` semantic provider.",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Documents processed | {totals['documents_processed']} |",
        f"| Input bytes | {totals['input_bytes']:,} |",
        f"| Field accuracy against ground truth | "
        f"{totals['fields_correct']}/{totals['fields_checked']} "
        f"({totals['field_accuracy']:.1%}) |",
        f"| Scanned-receipt field accuracy (OCR) | "
        f"{totals['ocr_fields_correct']}/{totals['ocr_fields_checked']} "
        f"({totals['ocr_field_accuracy']:.1%}) |",
        f"| Seeded defects detected | "
        f"{totals['expected_findings'] - totals['findings_missed']}/"
        f"{totals['expected_findings']} ({totals['recall']:.1%}) |",
        f"| Findings not in the expectation | {totals['findings_unexpected']} |",
        f"| Replay is a no-op | {'yes' if totals['replay_stable'] else 'no'} |",
        f"| Wall clock for the whole run | {payload['total_seconds']}s |",
        "",
    ]

    for result in payload["dossiers"]:
        lines += [
            f"## {result['reference']}",
            "",
            f"- documents: {result['documents_accepted']} accepted, "
            f"{result['documents_refused']} refused at intake, "
            f"{result['documents_processed']} read",
            f"- extractions: {result['extractions']}, segments indexed: {result['chunks']}",
            f"- fields needing human review: {result['needs_review_fields']}",
            f"- final state: {result['final_status']}",
            f"- stage seconds: {result['stage_seconds']}",
            f"- peak traced memory: {result['peak_memory_mb']} MB",
            f"- replay: {'stable' if result['replay_stable'] else 'CHANGED'} "
            f"in {result['replay_seconds']}s",
            "",
        ]
        if result["missed_findings"]:
            lines.append(f"- **defects not detected**: {', '.join(result['missed_findings'])}")
        if result["unexpected_findings"]:
            lines.append(
                f"- findings beyond the expectation: {', '.join(result['unexpected_findings'])}"
            )
        wrong = [f for f in result["fields"] if not f["correct"]]
        if wrong:
            lines.append("")
            lines.append("| Field not matched | Expected | Extracted |")
            lines.append("| --- | --- | --- |")
            for entry in wrong:
                lines.append(
                    f"| `{entry['field_path']}` | {entry['expected']} | "
                    f"{entry['actual'] if entry['actual'] is not None else 'not found'} |"
                )
        lines.append("")
        lines.append("| Receipt | Hard to read | Fields read correctly | Not read |")
        lines.append("| --- | --- | --- | --- |")
        for invoice in result["invoices"]:
            not_read = ", ".join(invoice["missing_fields"] + invoice["wrong_fields"]) or "-"
            lines.append(
                f"| {invoice['invoice_number']} | "
                f"{'yes' if invoice['hard_to_read'] else 'no'} | "
                f"{invoice['fields_correct']}/{invoice['fields_expected']} | {not_read} |"
            )
        lines.append("")

    lines += [
        "## Cost and saving scenarios",
        "",
        "Parameterised, not measured. No figure below comes from any real "
        "organisation, and the pipeline made no billable model call.",
        "",
        "| Dossiers/year | Manual minutes | Reviewer EUR/h | Share removed | "
        "Hours removed/year | Gross EUR/year |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario in payload["savings_scenarios"]:
        lines.append(
            f"| {scenario['dossiers_per_year']:,} | "
            f"{scenario['manual_minutes_per_dossier']} | "
            f"{scenario['reviewer_cost_eur_per_hour']:.0f} | "
            f"{scenario['share_of_time_removed']:.0%} | "
            f"{scenario['reviewer_hours_removed_per_year']:,.0f} | "
            f"{scenario['gross_reduction_eur_per_year']:,.0f} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
