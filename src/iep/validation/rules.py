"""The rule catalogue.

Every rule here is deterministic, versioned and pure: it reads the extractions
and the reference data and returns findings. No rule calls a model, and no
finding is produced from a value a model proposed - the amounts these rules
compare come from the PDF text layer, from workbook cells, from OCR, or from
the registry, all of which carry a locator a reviewer can open.

A finding carries a `fingerprint` built from the rule and its subject. Running
validation again on the same evidence therefore refreshes the same row rather
than appending a near-duplicate, which is what lets a reviewer's decision
survive a re-run.

The thresholds are plausible rather than authoritative. Real eligibility rules
differ by programme and by country, and `docs/validation-strategy.md` says so
explicitly: what is being demonstrated is the mechanism, not a legal ruleset.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from iep.connectors.registry import RegistryPerson
from iep.db.models import Document, Dossier, Extraction
from iep.domain.enums import DocumentKind, DocumentStatus, Severity

RULES_VERSION = "1.0.0"

# Amounts are compared to the cent. Anything looser hides real differences.
AMOUNT_TOLERANCE = Decimal("0.01")
MAX_MONTHLY_HOURS = Decimal("180")
MAX_ANNUAL_HOURS = Decimal("1720")
# Tesseract's own 0-100 scale. Below this the document is not trusted.
MIN_MEAN_OCR_CONFIDENCE = 78.0

_INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(all\s+)?(prior|previous)",
    r"you\s+are\s+now\s+in\s+\w+\s+mode",
    r"\bsystem\s*:",
    r"</?document>",
    r"assistant\s*:",
    r"approve\s+(this\s+)?dossier",
    r"delete\s+the\s+audit",
    r"set\s+every\s+finding",
)
_INJECTION = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class RuleFinding:
    rule_id: str
    severity: Severity
    message: str
    detail: dict[str, object] = field(default_factory=dict)
    extraction_ids: tuple[uuid.UUID, ...] = ()
    document_ids: tuple[uuid.UUID, ...] = ()
    # Distinguishes two findings from the same rule about different subjects.
    subject: str = ""

    @property
    def fingerprint(self) -> str:
        raw = f"{self.rule_id}|{RULES_VERSION}|{self.subject}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


@dataclass(frozen=True)
class CallWindow:
    eligible_from: date | None
    eligible_to: date | None
    source: str


@dataclass
class RuleContext:
    dossier: Dossier
    documents: list[Document]
    extractions: list[Extraction]
    registry: dict[str, RegistryPerson]
    call_window: CallWindow
    ocr_confidence_by_document: dict[uuid.UUID, float]
    document_text_by_id: dict[uuid.UUID, str]
    duplicate_document_shas: tuple[str, ...] = ()

    def by_field(self, field_path: str) -> list[Extraction]:
        return [e for e in self.extractions if e.field_path == field_path]

    def one(self, field_path: str) -> Extraction | None:
        found = self.by_field(field_path)
        return found[0] if found else None

    def documents_of_kind(self, kind: DocumentKind) -> list[Document]:
        return [d for d in self.documents if d.document_kind == kind]

    def per_document(self, document_id: uuid.UUID) -> dict[str, Extraction]:
        return {e.field_path: e for e in self.extractions if e.document_id == document_id}

    def timesheet_rows(self) -> list[dict[str, Extraction]]:
        grouped: dict[str, dict[str, Extraction]] = defaultdict(dict)
        for extraction in self.extractions:
            if not extraction.field_path.startswith("timesheet.rows["):
                continue
            prefix, _, suffix = extraction.field_path.rpartition(".")
            grouped[prefix][suffix] = extraction
        return [grouped[key] for key in sorted(grouped, key=_row_ordinal)]


def money(value: Decimal | None) -> str:
    """Amounts in a message read like money, not like a database column.

    A Numeric(16,4) column renders 83500.0000; a reviewer reads 83500.00.
    """
    if value is None:
        return "-"
    return f"{value:.2f}"


def hours_text(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}"


def _row_ordinal(prefix: str) -> int:
    match = re.search(r"\[(\d+)\]", prefix)
    return int(match.group(1)) if match else 0


# --------------------------------------------------------------------------
# Document-level rules
# --------------------------------------------------------------------------


def rule_document_intake(ctx: RuleContext) -> Iterator[RuleFinding]:
    for document in ctx.documents:
        if document.status == DocumentStatus.UNSUPPORTED:
            yield RuleFinding(
                rule_id="UNSUPPORTED_DOCUMENT",
                severity=Severity.WARNING,
                message=(
                    f"{document.original_filename} was not accepted: "
                    f"{document.rejection_reason or 'unsupported format'}."
                ),
                document_ids=(document.id,),
                subject=str(document.id),
            )
        elif document.status == DocumentStatus.CORRUPT:
            yield RuleFinding(
                rule_id="CORRUPT_DOCUMENT",
                severity=Severity.WARNING,
                message=(
                    f"{document.original_filename} could not be read: "
                    f"{document.rejection_reason or 'corrupt file'}."
                ),
                document_ids=(document.id,),
                subject=str(document.id),
            )

    for digest in ctx.duplicate_document_shas:
        yield RuleFinding(
            rule_id="DUPLICATE_DOCUMENT",
            severity=Severity.INFO,
            message="A document with identical content was submitted more than once.",
            detail={"content_sha256": digest},
            subject=digest,
        )


def rule_ocr_confidence(ctx: RuleContext) -> Iterator[RuleFinding]:
    for document_id, confidence in ctx.ocr_confidence_by_document.items():
        if confidence >= MIN_MEAN_OCR_CONFIDENCE:
            continue
        document = next((d for d in ctx.documents if d.id == document_id), None)
        name = document.original_filename if document else str(document_id)
        yield RuleFinding(
            rule_id="LOW_OCR_CONFIDENCE",
            severity=Severity.WARNING,
            message=(
                f"{name} was read at {confidence:.1f}% mean OCR confidence, below the "
                f"{MIN_MEAN_OCR_CONFIDENCE:.0f}% threshold. Values from it need checking."
            ),
            detail={"mean_word_confidence": round(confidence, 2)},
            document_ids=(document_id,),
            subject=str(document_id),
        )


def rule_prompt_injection(ctx: RuleContext) -> Iterator[RuleFinding]:
    """Report documents that try to instruct the pipeline.

    Detection is not the defence - the defence is that document text never
    becomes an instruction and never becomes an amount. This rule exists so a
    reviewer is told that somebody tried, which is itself a fact about the
    dossier worth recording.
    """
    for document_id, text in ctx.document_text_by_id.items():
        matches = sorted({m.group(0).strip().lower() for m in _INJECTION.finditer(text)})
        if not matches:
            continue
        document = next((d for d in ctx.documents if d.id == document_id), None)
        name = document.original_filename if document else str(document_id)
        yield RuleFinding(
            rule_id="PROMPT_INJECTION_ATTEMPT",
            severity=Severity.WARNING,
            message=(
                f"{name} contains text addressed to an automated reader. It was processed as "
                "data and changed nothing, but the document should be looked at."
            ),
            detail={"matched_phrases": matches[:10]},
            document_ids=(document_id,),
            subject=str(document_id),
        )


# --------------------------------------------------------------------------
# Invoice rules
# --------------------------------------------------------------------------


def rule_invoice_project_code(ctx: RuleContext) -> Iterator[RuleFinding]:
    for document in ctx.documents_of_kind(DocumentKind.EXPENSE_INVOICE):
        fields = ctx.per_document(document.id)
        code = fields.get("invoice.project_code")
        if code is None or not (code.value_text or "").strip("- "):
            yield RuleFinding(
                rule_id="MISSING_PROJECT_CODE",
                severity=Severity.BLOCKER,
                message=(
                    f"{document.original_filename} carries no project reference, so it cannot "
                    "be attributed to this dossier."
                ),
                document_ids=(document.id,),
                subject=str(document.id),
            )
            continue
        value = (code.value_text or "").strip().upper()
        if ctx.dossier.reference.upper() not in value:
            yield RuleFinding(
                rule_id="PROJECT_CODE_MISMATCH",
                severity=Severity.BLOCKER,
                message=(
                    f"{document.original_filename} references {value!r}, not "
                    f"{ctx.dossier.reference}."
                ),
                detail={"found": value, "expected": ctx.dossier.reference},
                extraction_ids=(code.id,),
                document_ids=(document.id,),
                subject=str(document.id),
            )


def rule_duplicate_invoice_number(ctx: RuleContext) -> Iterator[RuleFinding]:
    seen: dict[str, list[Extraction]] = defaultdict(list)
    for document in ctx.documents_of_kind(DocumentKind.EXPENSE_INVOICE):
        number = ctx.per_document(document.id).get("invoice.number")
        if number is None:
            continue
        key = _normalise_reference(number.value_text or "")
        if key:
            seen[key].append(number)

    for key, extractions in sorted(seen.items()):
        if len(extractions) < 2:
            continue
        documents = tuple(e.document_id for e in extractions if e.document_id is not None)
        yield RuleFinding(
            rule_id="DUPLICATE_INVOICE_NUMBER",
            severity=Severity.BLOCKER,
            message=(
                f"Invoice number {key} appears on {len(extractions)} separate documents. "
                "The same expense may be claimed twice."
            ),
            detail={"invoice_number": key, "occurrences": len(extractions)},
            extraction_ids=tuple(e.id for e in extractions),
            document_ids=documents,
            subject=key,
        )


def rule_invoice_dates(ctx: RuleContext) -> Iterator[RuleFinding]:
    window_start = ctx.call_window.eligible_from or ctx.dossier.period_start
    window_end = ctx.call_window.eligible_to or ctx.dossier.period_end
    start = max(window_start, ctx.dossier.period_start)
    end = min(window_end, ctx.dossier.period_end)

    for document in ctx.documents_of_kind(DocumentKind.EXPENSE_INVOICE):
        issued = ctx.per_document(document.id).get("invoice.issue_date")
        if issued is None or issued.value_date is None:
            continue
        if start <= issued.value_date <= end:
            continue
        yield RuleFinding(
            rule_id="EXPENSE_OUTSIDE_ELIGIBLE_PERIOD",
            severity=Severity.BLOCKER,
            message=(
                f"{document.original_filename} is dated {issued.value_date.isoformat()}, outside "
                f"the eligible window {start.isoformat()} to {end.isoformat()}."
            ),
            detail={
                "issue_date": issued.value_date.isoformat(),
                "eligible_from": start.isoformat(),
                "eligible_to": end.isoformat(),
                "window_source": ctx.call_window.source,
            },
            extraction_ids=(issued.id,),
            document_ids=(document.id,),
            subject=str(document.id),
        )


def rule_invoice_arithmetic(ctx: RuleContext) -> Iterator[RuleFinding]:
    for document in ctx.documents_of_kind(DocumentKind.EXPENSE_INVOICE):
        fields = ctx.per_document(document.id)
        base = fields.get("invoice.base_eur")
        vat = fields.get("invoice.vat_eur")
        total = fields.get("invoice.total_eur")
        if not (base and vat and total):
            continue
        if base.value_number is None or vat.value_number is None or total.value_number is None:
            continue
        expected = base.value_number + vat.value_number
        if abs(expected - total.value_number) <= AMOUNT_TOLERANCE:
            continue
        yield RuleFinding(
            rule_id="INVOICE_ARITHMETIC_MISMATCH",
            severity=Severity.WARNING,
            message=(
                f"{document.original_filename}: base plus VAT is {money(expected)}, but the "
                f"stated total is {money(total.value_number)}."
            ),
            detail={
                "base_eur": str(base.value_number),
                "vat_eur": str(vat.value_number),
                "stated_total_eur": str(total.value_number),
                "computed_total_eur": str(expected),
            },
            extraction_ids=(base.id, vat.id, total.id),
            document_ids=(document.id,),
            subject=str(document.id),
        )


# --------------------------------------------------------------------------
# Timesheet and personnel rules
# --------------------------------------------------------------------------


def rule_timesheet_rows(ctx: RuleContext) -> Iterator[RuleFinding]:
    annual_hours: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    for row in ctx.timesheet_rows():
        employee = row.get("employee_id")
        hours = row.get("hours")
        rate = row.get("hourly_rate_eur")
        amount = row.get("amount_eur")
        month = row.get("month")
        if employee is None or hours is None or hours.value_number is None:
            continue
        employee_id = (employee.value_text or "").strip()
        row_hours = f"{hours.value_number:g}"
        month_text = (month.value_text or "").strip() if month else ""
        subject = f"{employee_id}|{month_text}"

        if hours.value_number < 0:
            yield RuleFinding(
                rule_id="NEGATIVE_HOURS",
                severity=Severity.BLOCKER,
                message=(
                    f"{employee_id} has {row_hours} hours recorded for {month_text}. "
                    "Negative hours cannot be claimed."
                ),
                detail={"employee_id": employee_id, "month": month_text},
                extraction_ids=(hours.id,),
                document_ids=_document_ids(hours),
                subject=subject,
            )
        elif hours.value_number > MAX_MONTHLY_HOURS:
            yield RuleFinding(
                rule_id="HOURS_ABOVE_MONTHLY_CEILING",
                severity=Severity.WARNING,
                message=(
                    f"{employee_id} has {row_hours} hours in {month_text}, above the "
                    f"{MAX_MONTHLY_HOURS:g} hour monthly ceiling used here."
                ),
                detail={"employee_id": employee_id, "month": month_text},
                extraction_ids=(hours.id,),
                document_ids=_document_ids(hours),
                subject=subject,
            )

        if hours.value_number > 0 and month_text[:4].isdigit():
            annual_hours[(employee_id, month_text[:4])] += hours.value_number

        if (
            rate is not None
            and rate.value_number is not None
            and amount is not None
            and amount.value_number is not None
        ):
            expected = (hours.value_number * rate.value_number).quantize(Decimal("0.01"))
            if abs(expected - amount.value_number) > AMOUNT_TOLERANCE:
                yield RuleFinding(
                    rule_id="TIMESHEET_ROW_ARITHMETIC",
                    severity=Severity.WARNING,
                    message=(
                        f"{employee_id} {month_text}: {row_hours} hours at "
                        f"{money(rate.value_number)} is {money(expected)}, but the row states "
                        f"{money(amount.value_number)}."
                    ),
                    extraction_ids=(hours.id, rate.id, amount.id),
                    document_ids=_document_ids(amount),
                    subject=subject,
                )

        person = ctx.registry.get(employee_id)
        if person is None:
            yield RuleFinding(
                rule_id="UNKNOWN_PERSON",
                severity=Severity.BLOCKER,
                message=(
                    f"{employee_id} is not in the personnel registry, so their hours cannot be "
                    "attributed."
                ),
                detail={"employee_id": employee_id},
                extraction_ids=(employee.id,),
                document_ids=_document_ids(employee),
                subject=employee_id,
            )
            continue

        if (
            rate is not None
            and rate.value_number is not None
            and abs(rate.value_number - person.hourly_rate_eur) > AMOUNT_TOLERANCE
        ):
            yield RuleFinding(
                rule_id="PERSONNEL_RATE_MISMATCH",
                severity=Severity.BLOCKER,
                message=(
                    f"{employee_id} is charged at {money(rate.value_number)} EUR/h but the "
                    f"registry holds {money(person.hourly_rate_eur)} EUR/h."
                ),
                detail={
                    "employee_id": employee_id,
                    "declared_rate_eur": str(rate.value_number),
                    "registry_rate_eur": str(person.hourly_rate_eur),
                },
                extraction_ids=(rate.id,),
                document_ids=_document_ids(rate),
                subject=f"{employee_id}|rate",
            )

        month_date = _month_start(month_text)
        if month_date is not None and not person.covers(month_date):
            yield RuleFinding(
                rule_id="PERSON_OUTSIDE_CONTRACT",
                severity=Severity.BLOCKER,
                message=(
                    f"{employee_id} has hours in {month_text}, outside their registered contract "
                    f"({person.contract_start.isoformat()} to "
                    f"{person.contract_end.isoformat() if person.contract_end else 'open'})."
                ),
                detail={
                    "employee_id": employee_id,
                    "month": month_text,
                    "contract_start": person.contract_start.isoformat(),
                    "contract_end": person.contract_end.isoformat()
                    if person.contract_end
                    else None,
                },
                extraction_ids=(employee.id,),
                document_ids=_document_ids(employee),
                subject=subject,
            )

    for (employee_id, year), total in sorted(annual_hours.items()):
        if total > MAX_ANNUAL_HOURS:
            yield RuleFinding(
                rule_id="HOURS_ABOVE_ANNUAL_CEILING",
                severity=Severity.WARNING,
                message=(
                    f"{employee_id} accumulates {hours_text(total)} hours in {year}, above the "
                    f"{MAX_ANNUAL_HOURS:g} hour annual ceiling used here."
                ),
                detail={"employee_id": employee_id, "year": year, "hours": str(total)},
                subject=f"{employee_id}|{year}",
            )


# --------------------------------------------------------------------------
# Reconciliation across sources
# --------------------------------------------------------------------------


def rule_cost_reconciliation(ctx: RuleContext) -> Iterator[RuleFinding]:
    declared_personnel = ctx.one("report.declared_personnel_cost_eur")
    declared_external = ctx.one("report.declared_external_cost_eur")
    declared_total = ctx.one("report.declared_total_eur")
    timesheet_total = ctx.one("timesheet.total_amount_eur")
    invoice_total = ctx.one("invoices.total_eur")

    if declared_personnel is not None and timesheet_total is not None:
        yield from _compare(
            "PERSONNEL_COST_MISMATCH",
            "The report declares {a} of personnel cost; the timesheet adds up to {b}.",
            declared_personnel,
            timesheet_total,
            Severity.BLOCKER,
        )

    if declared_external is not None and invoice_total is not None:
        yield from _compare(
            "EXTERNAL_COST_MISMATCH",
            "The report declares {a} of external cost; the receipts add up to {b}.",
            declared_external,
            invoice_total,
            Severity.BLOCKER,
        )

    if declared_total is not None and declared_total.value_number is not None:
        claimed = ctx.dossier.claimed_total_eur
        if abs(declared_total.value_number - claimed) > AMOUNT_TOLERANCE:
            yield RuleFinding(
                rule_id="CLAIMED_TOTAL_MISMATCH",
                severity=Severity.BLOCKER,
                message=(
                    f"The dossier claims {money(claimed)} but the report states a total of "
                    f"{money(declared_total.value_number)}."
                ),
                detail={
                    "claimed_total_eur": str(claimed),
                    "report_total_eur": str(declared_total.value_number),
                },
                extraction_ids=(declared_total.id,),
                subject="claimed_total",
            )


def rule_required_evidence(ctx: RuleContext) -> Iterator[RuleFinding]:
    required = {
        DocumentKind.TECHNICAL_REPORT: "a technical report",
        DocumentKind.TIMESHEET: "a timesheet",
        DocumentKind.EXPENSE_INVOICE: "at least one expense receipt",
    }
    for kind, description in required.items():
        if not ctx.documents_of_kind(kind):
            yield RuleFinding(
                rule_id="INSUFFICIENT_EVIDENCE",
                severity=Severity.BLOCKER,
                message=f"The dossier has no readable document providing {description}.",
                detail={"missing_kind": str(kind)},
                subject=str(kind),
            )

    for field_path in (
        "report.declared_personnel_cost_eur",
        "report.declared_external_cost_eur",
        "report.declared_total_eur",
    ):
        if ctx.one(field_path) is None and ctx.documents_of_kind(DocumentKind.TECHNICAL_REPORT):
            yield RuleFinding(
                rule_id="INSUFFICIENT_EVIDENCE",
                severity=Severity.BLOCKER,
                message=f"The technical report does not state {field_path.rsplit('.', 1)[-1]}.",
                detail={"missing_field": field_path},
                subject=field_path,
            )


def rule_ambiguous_fields(ctx: RuleContext) -> Iterator[RuleFinding]:
    """Two readings of the same field on the same document that disagree."""
    grouped: dict[tuple[uuid.UUID | None, str], list[Extraction]] = defaultdict(list)
    for extraction in ctx.extractions:
        grouped[(extraction.document_id, extraction.field_path)].append(extraction)

    for (document_id, field_path), extractions in sorted(
        grouped.items(), key=lambda item: (str(item[0][0]), item[0][1])
    ):
        distinct = {
            (e.value_text or "", str(e.value_number), str(e.value_date)) for e in extractions
        }
        if len(distinct) < 2:
            continue
        yield RuleFinding(
            rule_id="AMBIGUOUS_FIELD",
            severity=Severity.WARNING,
            message=(
                f"{field_path} was read {len(distinct)} different ways from the same document. "
                "A reviewer has to choose."
            ),
            detail={"field_path": field_path, "readings": sorted(str(d) for d in distinct)[:5]},
            extraction_ids=tuple(e.id for e in extractions),
            document_ids=(document_id,) if document_id else (),
            subject=f"{document_id}|{field_path}",
        )


ALL_RULES = (
    rule_document_intake,
    rule_ocr_confidence,
    rule_prompt_injection,
    rule_invoice_project_code,
    rule_duplicate_invoice_number,
    rule_invoice_dates,
    rule_invoice_arithmetic,
    rule_timesheet_rows,
    rule_cost_reconciliation,
    rule_required_evidence,
    rule_ambiguous_fields,
)


def evaluate(ctx: RuleContext) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    for rule in ALL_RULES:
        findings.extend(rule(ctx))
    return findings


def _compare(
    rule_id: str,
    template: str,
    declared: Extraction,
    computed: Extraction,
    severity: Severity,
) -> Iterable[RuleFinding]:
    if declared.value_number is None or computed.value_number is None:
        return
    if abs(declared.value_number - computed.value_number) <= AMOUNT_TOLERANCE:
        return
    yield RuleFinding(
        rule_id=rule_id,
        severity=severity,
        message=template.format(a=money(declared.value_number), b=money(computed.value_number)),
        detail={
            "declared_eur": str(declared.value_number),
            "evidence_eur": str(computed.value_number),
            "difference_eur": str(declared.value_number - computed.value_number),
        },
        extraction_ids=(declared.id, computed.id),
        subject=rule_id.lower(),
    )


def _document_ids(extraction: Extraction) -> tuple[uuid.UUID, ...]:
    return (extraction.document_id,) if extraction.document_id else ()


def _normalise_reference(value: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "", value.upper())


def _month_start(month_text: str) -> date | None:
    match = re.match(r"^(\d{4})-(\d{2})$", month_text.strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), 1)
    except ValueError:
        return None
