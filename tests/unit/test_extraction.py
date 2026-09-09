"""Parsing, workbook reading and PDF text extraction."""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from corpus import documents as corpus_documents
from corpus.dataset import DOSSIER_A
from openpyxl import Workbook
from PIL import Image

from iep.extraction import excel, ocr, parse, pdf_text
from iep.extraction.base import ExtractionError
from iep.extraction.fields import report_fields, split_period, timesheet_fields


class TestAmountParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("TOTAL FACTURA: 18.392,00 EUR", Decimal("18392.00")),
            ("Base imponible: 15.200,00 EUR", Decimal("15200.00")),
            ("1.234.567,89", Decimal("1234567.89")),
            ("Importe 640", Decimal("640.00")),
            ("9 640,00", Decimal("9640.00")),
            ("sin importe", None),
        ],
    )
    def test_spanish_amounts(self, text: str, expected: Decimal | None) -> None:
        assert parse.parse_amount(text) == expected

    def test_ocr_repair_runs_before_the_strict_reader(self) -> None:
        # The strict reader would "succeed" on this with 1,00, which is a
        # thousand-fold error that never announces itself.
        assert parse.parse_amount(" 1S.20O,00") == Decimal("1.00")
        assert parse.parse_amount_tolerant(" 1S.20O,00") == Decimal("15200.00")

    def test_repair_does_not_damage_a_clean_reading(self) -> None:
        assert parse.parse_amount_tolerant("TOTAL: 11.664,40 EUR") == Decimal("11664.40")


class TestDateParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Fecha: 2025-04-22", date(2025, 4, 22)),
            ("14/11/2024", date(2024, 11, 14)),
            ("3 de marzo de 2025", date(2025, 3, 3)),
            ("31.12.2025", date(2025, 12, 31)),
            ("sin fecha", None),
            ("2025-13-45", None),
        ],
    )
    def test_dates(self, text: str, expected: date | None) -> None:
        assert parse.parse_date(text) == expected

    def test_day_comes_first(self) -> None:
        # 04/03/2025 is 4 March in these documents, not 3 April. Reading it the
        # other way puts half the corpus in the wrong month silently.
        assert parse.parse_date("04/03/2025") == date(2025, 3, 4)

    def test_period_line_splits_into_two_dates(self) -> None:
        assert split_period("2025-01-01 a 2025-12-31") == (date(2025, 1, 1), date(2025, 12, 31))
        assert split_period("2025-01-01") == (None, None)


class TestWorkbookReading:
    def _workbook(self, rows: list[list[object]], *, formula: str | None = None) -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        for row in rows:
            sheet.append(row)
        if formula:
            sheet["H1"] = formula
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def test_formulas_are_reported_not_evaluated(self) -> None:
        data = self._workbook([["a", 1]], formula="=1+1")
        sheets = excel.read_workbook(data, max_cells=1000)
        assert sheets[0].formula_cells == ("Sheet!H1",)
        values = [cell.value for row in sheets[0].rows for cell in row]
        assert 2 not in values  # the formula was never computed

    def test_cell_budget_is_enforced(self) -> None:
        data = self._workbook([list(range(20)) for _ in range(20)])
        with pytest.raises(ExtractionError, match="cell budget"):
            excel.read_workbook(data, max_cells=50)

    def test_a_non_workbook_is_refused(self) -> None:
        with pytest.raises(ExtractionError):
            excel.read_workbook(b"not a workbook", max_cells=100)

    def test_every_value_carries_its_cell(self) -> None:
        data = corpus_documents.timesheet_workbook(DOSSIER_A)
        sheets = excel.read_workbook(data, max_cells=100_000)
        candidates = timesheet_fields(sheets, extractor_version="test/1")
        assert candidates
        for candidate in candidates:
            locator = candidate.locator.model_dump()
            assert locator["kind"] == "EXCEL_CELL"
            assert locator["sheet"] == "Partes horarios"
            assert locator["cell"]

    def test_row_count_matches_the_source(self) -> None:
        data = corpus_documents.timesheet_workbook(DOSSIER_A)
        sheets = excel.read_workbook(data, max_cells=100_000)
        candidates = timesheet_fields(sheets, extractor_version="test/1")
        employee_ids = [c for c in candidates if c.field_path.endswith(".employee_id")]
        assert len(employee_ids) == len(DOSSIER_A.timesheet)

    def test_formula_backed_hours_are_not_used_as_evidence(self) -> None:
        row = tuple(
            excel.Cell("Partes horarios", 2, column, value)
            for column, value in enumerate(
                ("EMP-0001", "Person", "Engineer", "2025-01", 160, 40, 6400),
                start=1,
            )
        )
        header = tuple(
            excel.Cell("Partes horarios", 1, column, value)
            for column, value in enumerate(
                (
                    "ID empleado",
                    "Nombre",
                    "Rol",
                    "Mes",
                    "Horas",
                    "Tarifa EUR/h",
                    "Importe EUR",
                ),
                start=1,
            )
        )
        sheet = excel.Sheet(
            name="Partes horarios",
            rows=(header, row),
            formula_cells=("Partes horarios!E2",),
        )
        assert timesheet_fields([sheet], extractor_version="test/1") == []


class TestOcrBounds:
    def test_tesseract_timeout_is_a_retryable_extraction_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buffer = io.BytesIO()
        Image.new("L", (20, 20), color=255).save(buffer, format="PNG")
        monkeypatch.setattr(
            "iep.extraction.ocr.pytesseract.image_to_data",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timeout")),
        )
        with pytest.raises(ExtractionError, match="OCR timed out") as excinfo:
            ocr.recognise(buffer.getvalue(), page=1, language="spa", timeout_seconds=0.1)
        assert excinfo.value.retryable is True


class TestPdfText:
    def test_a_native_text_pdf_is_read_without_ocr(self) -> None:
        data = corpus_documents.technical_report(DOSSIER_A)
        pages = pdf_text.read_pages(data)
        assert pages.has_usable_text_layer()
        assert DOSSIER_A.reference in pages.text

    def test_fields_carry_a_page_and_a_character_span(self) -> None:
        pages = pdf_text.read_pages(corpus_documents.technical_report(DOSSIER_A))
        found = {c.field_path: c for c in report_fields(pages, extractor_version="test/1")}
        assert found["report.project_code"].value_text == DOSSIER_A.reference
        locator = found["report.declared_total_eur"].locator.model_dump()
        assert locator["kind"] == "PDF_PAGE"
        assert locator["page"] == 1
        assert locator["char_start"] < locator["char_end"]

    def test_declared_totals_are_read_as_numbers(self) -> None:
        pages = pdf_text.read_pages(corpus_documents.technical_report(DOSSIER_A))
        found = {c.field_path: c for c in report_fields(pages, extractor_version="test/1")}
        assert found["report.declared_total_eur"].value_number == DOSSIER_A.declared_total_eur

    def test_a_corrupt_pdf_raises_rather_than_returning_nothing(self) -> None:
        with pytest.raises(ExtractionError):
            pdf_text.read_pages(corpus_documents.corrupt_pdf())

    def test_chunks_keep_their_page(self) -> None:
        pages = pdf_text.read_pages(corpus_documents.technical_report(DOSSIER_A))
        chunks = pdf_text.chunk_pages(pages)
        assert chunks
        assert all(chunk.locator.model_dump()["page"] >= 1 for chunk in chunks)
