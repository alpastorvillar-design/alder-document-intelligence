"""Workbook reading.

Two safety properties, both tested:

* formulas are never evaluated. openpyxl has no evaluation engine, so the risk
  is not code execution but silently reading a formula string as if it were a
  value. Values are read from the cache the writing application left behind
  (`data_only=True`); a second pass without that flag finds the cells that are
  formulas, and any of them whose cached value is missing is reported rather
  than guessed at;
* the cell budget is capped before iteration, so a sheet declaring a million
  rows cannot pin a worker.

The locator for every value is the sheet plus the A1 cell reference, which is
what a reviewer needs to open the file and look.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from iep.domain.contracts import ExcelCellLocator
from iep.extraction.base import ExtractionError

EXTRACTOR_VERSION = "excel-openpyxl/1.0.0"


@dataclass(frozen=True)
class Cell:
    sheet: str
    row: int
    column: int
    value: Any

    @property
    def column_letter(self) -> str:
        return get_column_letter(self.column)

    @property
    def reference(self) -> str:
        return f"{self.column_letter}{self.row}"

    def locator(self) -> ExcelCellLocator:
        return ExcelCellLocator(
            sheet=self.sheet, cell=self.reference, row=self.row, column=self.column_letter
        )

    @property
    def text(self) -> str:
        if self.value is None:
            return ""
        if isinstance(self.value, datetime):
            return self.value.date().isoformat()
        if isinstance(self.value, date):
            return self.value.isoformat()
        return str(self.value)

    def as_decimal(self) -> Decimal | None:
        if isinstance(self.value, bool) or self.value is None:
            return None
        if isinstance(self.value, (int, float)):
            return Decimal(str(self.value))
        try:
            return Decimal(str(self.value).replace(".", "").replace(",", "."))
        except (InvalidOperation, ValueError):
            return None


@dataclass(frozen=True)
class Sheet:
    name: str
    rows: tuple[tuple[Cell, ...], ...]
    formula_cells: tuple[str, ...]

    def cell(self, row: int, column: int) -> Cell | None:
        for candidate in self.rows[row - 1] if 0 < row <= len(self.rows) else ():
            if candidate.column == column:
                return candidate
        return None


def _formula_cells(data: bytes, *, max_cells: int) -> dict[str, set[str]]:
    """Cells holding a formula, found by reading without the value cache.

    With `data_only=True` a formula cell yields its cached result, or None when
    no cache exists - either way the formula itself is invisible. This pass
    exists so the reader can tell a reviewer which numbers are computed
    elsewhere rather than written down.
    """
    found: dict[str, set[str]] = {}
    try:
        workbook = load_workbook(
            io.BytesIO(data), data_only=False, read_only=True, keep_links=False
        )
    except Exception:
        return found
    budget = max_cells
    try:
        for worksheet in workbook.worksheets:
            for row_index, row in enumerate(worksheet.iter_rows(), start=1):
                for column_index, cell in enumerate(row, start=1):
                    budget -= 1
                    if budget < 0:
                        return found
                    value = getattr(cell, "value", None)
                    if isinstance(value, str) and value.startswith("="):
                        reference = f"{get_column_letter(column_index)}{row_index}"
                        found.setdefault(worksheet.title, set()).add(reference)
    finally:
        workbook.close()
    return found


def read_workbook(data: bytes, *, max_cells: int) -> list[Sheet]:
    try:
        # `data_only=True` returns the value cached by the application that last
        # saved the file; openpyxl never computes one. A file saved by a tool
        # that writes no cache therefore has no value here, which is reported.
        workbook = load_workbook(io.BytesIO(data), data_only=True, read_only=True, keep_links=False)
    except Exception as exc:
        raise ExtractionError(
            f"workbook could not be opened: {type(exc).__name__}", retryable=False
        ) from exc

    formulas_by_sheet = _formula_cells(data, max_cells=max_cells)
    sheets: list[Sheet] = []
    budget = max_cells
    try:
        for worksheet in workbook.worksheets:
            rows: list[tuple[Cell, ...]] = []
            formulas: list[str] = []
            for row_index, row in enumerate(worksheet.iter_rows(), start=1):
                cells: list[Cell] = []
                # Read-only mode yields EmptyCell placeholders with no row or
                # column attribute, so the column index comes from the position
                # in the row rather than from the cell object.
                for column_index, cell in enumerate(row, start=1):
                    budget -= 1
                    if budget < 0:
                        raise ExtractionError(
                            f"workbook exceeds the {max_cells} cell budget", retryable=False
                        )
                    value = getattr(cell, "value", None)
                    reference = f"{get_column_letter(column_index)}{row_index}"
                    if reference in formulas_by_sheet.get(worksheet.title, set()):
                        formulas.append(f"{worksheet.title}!{reference}")
                        if isinstance(value, str) and value.startswith("="):
                            # No cached result: the value is unknown, not the
                            # formula text.
                            value = None
                    cells.append(
                        Cell(
                            sheet=worksheet.title,
                            row=row_index,
                            column=column_index,
                            value=value,
                        )
                    )
                rows.append(tuple(cells))
            sheets.append(
                Sheet(name=worksheet.title, rows=tuple(rows), formula_cells=tuple(formulas))
            )
    finally:
        workbook.close()
    return sheets


def find_header_row(sheet: Sheet, expected: set[str]) -> int | None:
    """Row index whose cells cover `expected`, matched case-insensitively."""
    wanted = {value.strip().lower() for value in expected}
    for index, row in enumerate(sheet.rows, start=1):
        labels = {cell.text.strip().lower() for cell in row if cell.text}
        if wanted.issubset(labels):
            return index
    return None


def header_columns(sheet: Sheet, header_row: int) -> dict[str, Cell]:
    return {
        cell.text.strip().lower(): cell for cell in sheet.rows[header_row - 1] if cell.text.strip()
    }
