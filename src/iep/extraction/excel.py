"""Workbook reading.

Two safety properties, both tested:

* formulas are never evaluated. openpyxl does not have an evaluation engine, so
  the risk is not code execution but silently reading a formula string as if it
  were a value. Cells whose value is a formula are reported as unreadable
  rather than parsed;
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

    sheets: list[Sheet] = []
    budget = max_cells
    try:
        for worksheet in workbook.worksheets:
            rows: list[tuple[Cell, ...]] = []
            formulas: list[str] = []
            for row_index, row in enumerate(worksheet.iter_rows(), start=1):
                cells: list[Cell] = []
                for cell in row:
                    budget -= 1
                    if budget < 0:
                        raise ExtractionError(
                            f"workbook exceeds the {max_cells} cell budget", retryable=False
                        )
                    value = cell.value
                    if isinstance(value, str) and value.startswith("="):
                        # A formula string reached us, which means no cached
                        # value existed. Record it and treat the cell as empty.
                        formulas.append(
                            f"{worksheet.title}!{get_column_letter(cell.column or 1)}{row_index}"
                        )
                        value = None
                    cells.append(
                        Cell(
                            sheet=worksheet.title,
                            row=row_index,
                            column=cell.column or 1,
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
