"""Report and export.

The report is the deliverable a reviewer or an auditor reads, so it shows the
evidence rather than only the conclusion: every figure appears with where it
came from, and a corrected value appears next to what the machine originally
read.

Exports are written with the spreadsheet-injection guard applied, because a CSV
of extracted values is exactly the kind of file somebody opens in Excel.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.audit import service as audit
from iep.db.models import Document, Dossier, Extraction, Finding, Report, ReviewDecision
from iep.domain.enums import AuditAction, FieldStatus, FindingStatus, Severity

TEMPLATE_DIR = Path(__file__).parent / "templates"
REPORT_VERSION = "report/1.0.0"

# Leading characters a spreadsheet treats as the start of a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class RenderedReport:
    html: bytes
    content_sha256: str
    finding_count: int
    blocker_count: int
    needs_review_count: int


def sanitise_cell(value: object) -> str:
    """Neutralise a value that a spreadsheet would execute.

    Prefixing with an apostrophe keeps the text readable and stops Excel and
    LibreOffice interpreting it as a formula. Applied to every exported cell,
    not only to ones that look suspicious.
    """
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def format_number(value: Decimal | None) -> str:
    """Fixed-point, never scientific.

    `Decimal.normalize()` renders 31500.00 as 3.15E+4, which is the wrong thing
    to put in front of an auditor. Whole numbers print without decimals,
    everything else to the cent.
    """
    if value is None:
        return ""
    if value == value.to_integral_value():
        return f"{value:.0f}"
    return f"{value:.2f}"


_env.filters["number"] = format_number


def collect(session: Session, dossier_id: uuid.UUID) -> dict[str, Any]:
    dossier = session.get(Dossier, dossier_id)
    if dossier is None:
        raise LookupError(dossier_id)

    documents = list(
        session.execute(
            select(Document)
            .where(Document.dossier_id == dossier_id)
            .order_by(Document.received_at.asc())
        ).scalars()
    )
    extractions = list(
        session.execute(
            select(Extraction)
            .where(Extraction.dossier_id == dossier_id)
            .order_by(Extraction.field_path.asc())
        ).scalars()
    )
    findings = list(
        session.execute(
            select(Finding).where(Finding.dossier_id == dossier_id).order_by(Finding.rule_id.asc())
        ).scalars()
    )
    decisions = list(
        session.execute(
            select(ReviewDecision)
            .where(ReviewDecision.dossier_id == dossier_id)
            .order_by(ReviewDecision.created_at.asc())
        ).scalars()
    )

    names = {document.id: document.original_filename for document in documents}
    severity_order = {Severity.BLOCKER: 0, Severity.WARNING: 1, Severity.INFO: 2}

    return {
        "dossier": dossier,
        "documents": documents,
        "extractions": extractions,
        "findings": sorted(
            findings, key=lambda f: (severity_order.get(Severity(f.severity), 9), f.rule_id)
        ),
        "decisions": decisions,
        "document_names": names,
        "generated_at": datetime.now(UTC),
        "report_version": REPORT_VERSION,
        "open_findings": [f for f in findings if f.status == FindingStatus.OPEN],
        "needs_review": [e for e in extractions if e.status == FieldStatus.NEEDS_REVIEW],
        "corrections": [e for e in extractions if e.original_value_text is not None],
        "summary_rows": _summary_rows(extractions),
    }


def render_html(session: Session, dossier_id: uuid.UUID) -> RenderedReport:
    context = collect(session, dossier_id)
    html = _env.get_template("report.html").render(**context, describe=describe_locator)
    data = html.encode("utf-8")
    findings = context["findings"]
    return RenderedReport(
        html=data,
        content_sha256=hashlib.sha256(data).hexdigest(),
        finding_count=len(findings),
        blocker_count=sum(1 for f in findings if Severity(f.severity) is Severity.BLOCKER),
        needs_review_count=len(context["needs_review"]),
    )


def persist(
    session: Session, dossier_id: uuid.UUID, rendered: RenderedReport, *, report_root: Path
) -> Report:
    report_root.mkdir(parents=True, exist_ok=True)
    filename = f"{dossier_id}-{rendered.content_sha256[:12]}.html"
    (report_root / filename).write_bytes(rendered.html)

    dossier = session.get(Dossier, dossier_id)
    assert dossier is not None
    row = Report(
        id=uuid.uuid4(),
        dossier_id=dossier_id,
        content_sha256=rendered.content_sha256,
        storage_key=filename,
        dossier_status=str(dossier.status),
        finding_count=rendered.finding_count,
        blocker_count=rendered.blocker_count,
        needs_review_count=rendered.needs_review_count,
    )
    session.add(row)
    audit.record(
        session,
        action=AuditAction.REPORT_GENERATED,
        dossier_id=dossier_id,
        payload={
            "report_version": REPORT_VERSION,
            "content_sha256": rendered.content_sha256,
            "findings": rendered.finding_count,
            "blockers": rendered.blocker_count,
        },
    )
    return row


def export_json(session: Session, dossier_id: uuid.UUID) -> bytes:
    context = collect(session, dossier_id)
    dossier = context["dossier"]
    payload = {
        "report_version": REPORT_VERSION,
        "generated_at": context["generated_at"].isoformat(),
        "dossier": {
            "id": str(dossier.id),
            "reference": dossier.reference,
            "title": dossier.title,
            "status": str(dossier.status),
            "period_start": dossier.period_start.isoformat(),
            "period_end": dossier.period_end.isoformat(),
            "claimed_total_eur": str(dossier.claimed_total_eur),
        },
        "documents": [
            {
                "id": str(d.id),
                "filename": d.original_filename,
                "media_kind": str(d.media_kind),
                "document_kind": str(d.document_kind),
                "status": str(d.status),
                "source_kind": str(d.source_kind),
                "size_bytes": d.size_bytes,
                "content_sha256": d.content_sha256,
                "page_count": d.page_count,
            }
            for d in context["documents"]
        ],
        "extractions": [
            {
                "id": str(e.id),
                "document_id": str(e.document_id) if e.document_id else None,
                "field_path": e.field_path,
                "value_text": e.value_text,
                "value_number": str(e.value_number) if e.value_number is not None else None,
                "value_date": e.value_date.isoformat() if e.value_date else None,
                "locator": e.locator,
                "method": e.method,
                "extractor_version": e.extractor_version,
                "contract_version": e.contract_version,
                "confidence": float(e.confidence),
                "status": str(e.status),
                "original_value_text": e.original_value_text,
                "corrected_by": e.corrected_by,
                "correction_reason": e.correction_reason,
            }
            for e in context["extractions"]
        ],
        "findings": [
            {
                "id": str(f.id),
                "rule_id": f.rule_id,
                "rule_version": f.rule_version,
                "severity": f.severity,
                "status": str(f.status),
                "message": f.message,
                "detail": f.detail,
                "extraction_ids": f.extraction_ids,
                "document_ids": f.document_ids,
                "resolved_by": f.resolved_by,
                "resolution_note": f.resolution_note,
            }
            for f in context["findings"]
        ],
        "review_decisions": [
            {
                "action": d.action,
                "actor": d.actor,
                "reason": d.reason,
                "created_at": d.created_at.isoformat(),
            }
            for d in context["decisions"]
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def export_csv(session: Session, dossier_id: uuid.UUID) -> bytes:
    context = collect(session, dossier_id)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "field_path",
            "value",
            "confidence",
            "status",
            "method",
            "extractor_version",
            "document",
            "evidence",
            "original_value",
            "corrected_by",
        ]
    )
    for extraction in context["extractions"]:
        writer.writerow(
            [
                sanitise_cell(extraction.field_path),
                sanitise_cell(_display_value(extraction)),
                sanitise_cell(f"{float(extraction.confidence):.3f}"),
                sanitise_cell(extraction.status),
                sanitise_cell(extraction.method),
                sanitise_cell(extraction.extractor_version),
                sanitise_cell(context["document_names"].get(extraction.document_id, "")),
                sanitise_cell(describe_locator(extraction.locator)),
                sanitise_cell(extraction.original_value_text or ""),
                sanitise_cell(extraction.corrected_by or ""),
            ]
        )
    return buffer.getvalue().encode("utf-8")


def describe_locator(locator: dict[str, Any]) -> str:
    """A locator rendered the way a person would say it out loud."""
    kind = locator.get("kind")
    if kind == "PDF_PAGE":
        span = ""
        if locator.get("char_start") is not None:
            span = f", characters {locator['char_start']}-{locator['char_end']}"
        return f"page {locator.get('page')}{span}"
    if kind == "OCR_WORD_BOX":
        return (
            f"page {locator.get('page')}, box "
            f"({locator.get('left')},{locator.get('top')}) "
            f"{locator.get('width')}x{locator.get('height')}, "
            f"OCR confidence {float(locator.get('word_confidence', 0)):.0f}%"
        )
    if kind == "EXCEL_CELL":
        return f"sheet {locator.get('sheet')!r}, cell {locator.get('cell')}"
    if kind == "API_FIELD":
        return f"{locator.get('endpoint')} -> {locator.get('json_path')}"
    if kind == "HTML_SELECTOR":
        return f"{locator.get('url')} -> {locator.get('selector')}"
    if kind == "DERIVED":
        return f"computed: {locator.get('rule')}"
    return str(kind)


def _display_value(extraction: Extraction) -> str:
    if extraction.value_number is not None:
        return format_number(extraction.value_number)
    if extraction.value_date is not None:
        return extraction.value_date.isoformat()
    return extraction.value_text or ""


def _summary_rows(extractions: list[Extraction]) -> list[tuple[str, str]]:
    wanted = (
        ("report.declared_personnel_cost_eur", "Personnel cost declared in the report"),
        ("timesheet.total_amount_eur", "Personnel cost supported by the timesheet"),
        ("report.declared_external_cost_eur", "External cost declared in the report"),
        ("invoices.total_eur", "External cost supported by receipts"),
        ("report.declared_total_eur", "Total declared in the report"),
    )
    by_path = {e.field_path: e for e in extractions}
    rows: list[tuple[str, str]] = []
    for path, label in wanted:
        extraction = by_path.get(path)
        rows.append((label, _display_value(extraction) if extraction else "not found"))
    return rows
