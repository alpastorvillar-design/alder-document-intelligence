"""Indexing a workbook, which this pipeline read and then did not index.

The defect was one missing line and it was invisible from the screen: every
timesheet value had an extraction with a sheet-and-cell locator, so the review
table looked complete, while `document_chunks` held nothing from the workbook
at all. Asked what the timesheets contained, the copilot answered out of the
memoria's prose and cited `memoria-tecnica.pdf` - which was the only honest
citation available to it, because the spreadsheet was not in the index.

Counted on `INN-2025-041` before the fix: 12 segments per invoice scan, 2 for
the memoria, and 0 for `partes-horarios.xlsx`.
"""

from __future__ import annotations

from iep.extraction.excel import Cell, Sheet, chunk_sheets


def cell(sheet: str, row: int, column: int, value: object) -> Cell:
    return Cell(sheet=sheet, row=row, column=column, value=value)


def sheet(name: str, rows: list[list[object | None]], first_row: int = 1) -> Sheet:
    """A sheet from a grid, skipping `None` the way the reader does."""
    built = tuple(
        tuple(
            cell(name, first_row + r, c + 1, value)
            for c, value in enumerate(values)
            if value is not None
        )
        for r, values in enumerate(rows)
    )
    return Sheet(name=name, rows=built, formula_cells=())


TIMESHEET = sheet(
    "Partes horarios",
    [
        ["Expediente", "INN-2025-041"],
        ["Periodo", "2025-01-01 / 2025-12-31"],
        [],
        ["ID empleado", "Nombre", "Rol", "Mes", "Horas", "Tarifa EUR/h", "Importe EUR"],
        ["EMP-0142", "Nerea Talvi", "Investigadora principal", "2025-02", 120, 42.5, 5100],
        ["EMP-0203", "Marta Uxeli", "Ingeniera de software", "2025-03", 140, 34.75, 4865],
    ],
)


class TestARowIsTheUnit:
    def test_one_segment_per_filled_row(self) -> None:
        """Not per cell and not per sheet.

        A cell on its own is "120" with no idea what it counts. A whole sheet
        is one segment that matches every query and locates nothing.
        """
        chunks = chunk_sheets([TIMESHEET])
        assert len(chunks) == 5  # two title rows, the headers, two data rows

    def test_an_empty_row_produces_nothing(self) -> None:
        assert all("fila 3" not in chunk.text for chunk in chunk_sheets([TIMESHEET]))

    def test_ordinals_run_without_gaps_across_sheets(self) -> None:
        """The ordinal is the primary key with the document, so a repeat is a
        row that silently overwrites another."""
        chunks = chunk_sheets([TIMESHEET, sheet("Otra", [["a", "b"]])])
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


class TestEachValueIsNamedByItsColumn:
    def test_a_data_row_carries_its_headers(self) -> None:
        """ "Nerea Talvi 120 40 4800" is not retrievable by a question about
        hours. Naming each value is what makes the segment mean something."""
        row = next(chunk for chunk in chunk_sheets([TIMESHEET]) if "EMP-0142" in chunk.text)
        assert "Nombre: Nerea Talvi" in row.text
        assert "Horas: 120" in row.text
        assert "Tarifa EUR/h: 42.5" in row.text
        assert "Hoja «Partes horarios», fila 5" in row.text

    def test_a_title_line_reads_as_a_pair(self) -> None:
        chunks = chunk_sheets([TIMESHEET])
        assert "Expediente: INN-2025-041" in chunks[0].text

    def test_the_header_row_itself_is_indexed(self) -> None:
        """So "what columns does the sheet have" is answerable."""
        headers = next(chunk for chunk in chunk_sheets([TIMESHEET]) if "fila 4" in chunk.text)
        assert "ID empleado · Nombre · Rol" in headers.text

    def test_a_label_in_a_value_column_is_repeated_faithfully(self) -> None:
        """A TOTAL row puts a word under the rate column. Pairing it with that
        header says what the sheet says; guessing would be worse."""
        with_total = sheet(
            "Partes horarios",
            [
                ["ID empleado", "Nombre", "Horas", "Tarifa EUR/h", "Importe EUR"],
                ["EMP-0142", "Nerea Talvi", 120, 42.5, 5100],
                [None, None, None, "TOTAL", 19068.5],
            ],
        )
        total = next(chunk for chunk in chunk_sheets([with_total]) if "TOTAL" in chunk.text)
        assert "Tarifa EUR/h: TOTAL" in total.text
        assert "Importe EUR: 19068.5" in total.text

    def test_a_sheet_with_no_header_row_is_still_indexed(self) -> None:
        """Unlabelled is worse than labelled and far better than absent."""
        numbers = sheet("Datos", [[1, 2, 3], [4, 5, 6]])
        chunks = chunk_sheets([numbers])
        assert len(chunks) == 2
        assert "1 · 2 · 3" in chunks[0].text

    def test_three_text_cells_are_headers_and_two_are_a_title(self) -> None:
        """The rule that separates them, stated as a test: a header row has
        three or more filled cells and no numbers in it."""
        pair = sheet("H", [["Expediente", "INN-2025-041"], ["a", 1]])
        assert "Expediente: INN-2025-041" in chunk_sheets([pair])[0].text

        triple = sheet("H", [["Uno", "Dos", "Tres"], ["a", "b", 1]])
        assert "Uno: a" in chunk_sheets([triple])[1].text


class TestTheLocatorPointsAtTheRow:
    def test_it_is_the_first_filled_cell(self) -> None:
        """A row spans cells and the locator addresses one, so it names where
        the row starts - which is what a reviewer needs to find it."""
        row = next(chunk for chunk in chunk_sheets([TIMESHEET]) if "EMP-0142" in chunk.text)
        assert row.locator.cell == "A5"
        assert row.locator.sheet == "Partes horarios"
        assert row.locator.row == 5

    def test_a_row_that_starts_late_locates_where_it_starts(self) -> None:
        late = sheet("Partes horarios", [[None, None, "TOTAL", 19068.5]])
        assert chunk_sheets([late])[0].locator.cell == "C1"


class TestTheSegmentStaysBounded:
    def test_a_very_wide_row_is_truncated(self) -> None:
        wide = sheet("Ancha", [["x" * 400, "y" * 400, "z" * 400]])
        assert len(chunk_sheets([wide], max_chars=200)[0].text) == 200
