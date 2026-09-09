"""Turn a read document into field candidates.

Each document kind has its own reader, and each returns the same thing: values
with the exact place they were read from and a confidence that reflects how the
reading was obtained. Confidence is not decoration - it is what routes a value
to a human, so it is derived from the engine's own signal (OCR word confidence)
or from the determinism of the read (a workbook cell is not a guess), never
assigned by feel.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from iep.domain.contracts import ExcelCellLocator, PdfPageLocator
from iep.domain.enums import ExtractionMethod
from iep.extraction import excel, ocr, parse
from iep.extraction.base import FieldCandidate
from iep.extraction.excel import Sheet
from iep.extraction.ocr import OcrPage
from iep.extraction.pdf_text import PdfPages

# Confidence for a value read from a PDF text layer by an unambiguous label.
LABELLED_TEXT_CONFIDENCE = 0.95
# A workbook cell read directly. Not 1.0: the cell may hold the wrong thing.
CELL_CONFIDENCE = 0.98
# Penalty applied when an amount only parsed after OCR digit repair.
REPAIRED_PENALTY = 0.75

REPORT_LABELS: dict[str, str] = {
    "report.project_code": r"expediente",
    "report.call_code": r"convocatoria",
    "report.title": r"titulo\s+del\s+proyecto",
    "report.period": r"periodo\s+de\s+ejecucion",
    "report.declared_personnel_cost_eur": r"coste\s+de\s+personal\s+declarado",
    "report.declared_external_cost_eur": r"colaboraciones\s+externas\s+declaradas",
    "report.declared_total_eur": r"total\s+declarado",
}

INVOICE_LABELS: dict[str, str] = {
    "invoice.number": r"numero",
    "invoice.issue_date": r"fecha\s+de\s+emision",
    "invoice.supplier_name": r"proveedor",
    # The label is printed as "NIF (sintetico)"; the parenthetical is part
    # of the label, not of the value.
    "invoice.supplier_tax_id": r"nif(?:\s*\([^)]*\))?",
    "invoice.project_code": r"referencia\s+de\s+proyecto",
    "invoice.base_eur": r"base\s+imponible",
    "invoice.vat_eur": r"iva\s*\d{1,2}\s*%",
    "invoice.total_eur": r"total\s+factura",
}

AMOUNT_FIELDS = frozenset(
    {
        "report.declared_personnel_cost_eur",
        "report.declared_external_cost_eur",
        "report.declared_total_eur",
        "invoice.base_eur",
        "invoice.vat_eur",
        "invoice.total_eur",
    }
)
DATE_FIELDS = frozenset({"invoice.issue_date"})

TIMESHEET_HEADERS = {"id empleado", "mes", "horas", "tarifa eur/h", "importe eur"}


def report_fields(pages: PdfPages, *, extractor_version: str) -> list[FieldCandidate]:
    candidates: list[FieldCandidate] = []
    for page_number, page_text in enumerate(pages.pages, start=1):
        folded = _fold(page_text)
        for field_path, label in REPORT_LABELS.items():
            if any(c.field_path == field_path for c in candidates):
                continue
            found = parse.find_labelled(folded, label)
            if found is None:
                continue
            _, start, end = found
            raw = parse.normalise_whitespace(page_text[start:end])
            locator = PdfPageLocator(
                page=page_number, char_start=start, char_end=end, snippet=raw[:200]
            )
            candidates.extend(
                _typed_candidates(
                    field_path,
                    raw,
                    locator,
                    ExtractionMethod.PDF_TEXT,
                    extractor_version,
                    LABELLED_TEXT_CONFIDENCE,
                )
            )
    return candidates


def invoice_fields(pages: list[OcrPage], *, extractor_version: str) -> list[FieldCandidate]:
    candidates: list[FieldCandidate] = []
    for page in pages:
        for line in page.lines:
            folded = _fold(line.text)
            for field_path, label in INVOICE_LABELS.items():
                if any(c.field_path == field_path for c in candidates):
                    continue
                found = parse.find_labelled(folded, label)
                if found is None:
                    continue
                raw = parse.normalise_whitespace(line.text[found[1] : found[2]])
                if not raw:
                    continue
                # OCR confidence is the engine's own per-word score for the line
                # this value was read from, mapped onto 0-1.
                confidence = max(0.0, min(line.confidence / 100.0, 0.99))
                candidates.extend(
                    _typed_candidates(
                        field_path,
                        raw,
                        line.locator(page.page),
                        ExtractionMethod.OCR_TESSERACT,
                        extractor_version,
                        confidence,
                        tolerant=True,
                    )
                )
    return candidates


def timesheet_fields(sheets: list[Sheet], *, extractor_version: str) -> list[FieldCandidate]:
    candidates: list[FieldCandidate] = []
    for sheet in sheets:
        header_row = excel.find_header_row(sheet, TIMESHEET_HEADERS)
        if header_row is None:
            continue
        columns = {
            label: cell.column for label, cell in excel.header_columns(sheet, header_row).items()
        }
        row_index = 0
        for row in sheet.rows[header_row:]:
            values = {cell.column: cell for cell in row}
            formula_cells = set(sheet.formula_cells)
            employee_cell = _trusted_cell(
                sheet, values, columns.get("id empleado", -1), formula_cells
            )
            if employee_cell is None or not employee_cell.text.strip():
                continue
            hours_cell = _trusted_cell(sheet, values, columns.get("horas", -1), formula_cells)
            if hours_cell is None or hours_cell.as_decimal() is None:
                continue

            prefix = f"timesheet.rows[{row_index}]"
            candidates.append(_cell_text(f"{prefix}.employee_id", employee_cell, extractor_version))
            for label, suffix in (
                ("nombre", "full_name"),
                ("rol", "role"),
                ("mes", "month"),
            ):
                cell = _trusted_cell(sheet, values, columns.get(label, -1), formula_cells)
                if cell is not None and cell.text.strip():
                    candidates.append(_cell_text(f"{prefix}.{suffix}", cell, extractor_version))
            for label, suffix in (
                ("horas", "hours"),
                ("tarifa eur/h", "hourly_rate_eur"),
                ("importe eur", "amount_eur"),
            ):
                cell = _trusted_cell(sheet, values, columns.get(label, -1), formula_cells)
                number = cell.as_decimal() if cell is not None else None
                if cell is not None and number is not None:
                    candidates.append(
                        _cell_number(f"{prefix}.{suffix}", cell, number, extractor_version)
                    )
            row_index += 1
    return candidates


def _trusted_cell(
    sheet: Sheet,
    values: dict[int, excel.Cell],
    column: int,
    formula_cells: set[str],
) -> excel.Cell | None:
    """Return only literal cells; cached formula results are untrusted input."""
    cell = values.get(column)
    if cell is None or f"{sheet.name}!{cell.reference}" in formula_cells:
        return None
    return cell


def _cell_text(field_path: str, cell: excel.Cell, extractor_version: str) -> FieldCandidate:
    return FieldCandidate(
        field_path=field_path,
        locator=cell.locator(),
        method=ExtractionMethod.EXCEL_CELL,
        extractor_version=extractor_version,
        confidence=CELL_CONFIDENCE,
        value_text=cell.text[:500],
    )


def _cell_number(
    field_path: str, cell: excel.Cell, number: Decimal, extractor_version: str
) -> FieldCandidate:
    return FieldCandidate(
        field_path=field_path,
        locator=cell.locator(),
        method=ExtractionMethod.EXCEL_CELL,
        extractor_version=extractor_version,
        confidence=CELL_CONFIDENCE,
        value_text=cell.text[:500],
        value_number=number,
    )


def _typed_candidates(
    field_path: str,
    raw: str,
    locator: PdfPageLocator | ocr.OcrWordBoxLocator | ExcelCellLocator,
    method: ExtractionMethod,
    extractor_version: str,
    confidence: float,
    *,
    tolerant: bool = False,
) -> list[FieldCandidate]:
    value_number: Decimal | None = None
    value_date: date | None = None
    adjusted = confidence

    if field_path in AMOUNT_FIELDS:
        strict = parse.parse_amount(raw)
        value_number = parse.parse_amount_tolerant(raw) if tolerant else strict
        if value_number is None:
            return []
        if tolerant and strict != value_number:
            # The value only appeared after repairing OCR digit confusions, so
            # it is less trustworthy than a clean read of the same line.
            adjusted = confidence * REPAIRED_PENALTY
    elif field_path in DATE_FIELDS:
        value_date = parse.parse_date(raw)
        if value_date is None:
            return []

    return [
        FieldCandidate(
            field_path=field_path,
            locator=locator,
            method=method,
            extractor_version=extractor_version,
            confidence=round(min(max(adjusted, 0.0), 1.0), 3),
            value_text=raw[:500],
            value_number=value_number,
            value_date=value_date,
            context={"raw": raw[:200]},
        )
    ]


_ACCENTS = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")


def _fold(text: str) -> str:
    """Lower-case and strip accents, preserving character offsets."""
    return text.translate(_ACCENTS).lower()


def split_period(raw: str) -> tuple[date | None, date | None]:
    """Read `2025-01-01 a 2025-12-31` into its two endpoints."""
    cleaned = raw.replace(" al ", " a ")
    parts = [part for part in cleaned.split(" a ") if part.strip()]
    if len(parts) < 2:
        return None, None
    return parse.parse_date(parts[0]), parse.parse_date(parts[1])
