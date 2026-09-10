"""Turning a stored locator back into something a person can look at.

A locator is only worth storing if it can be walked backwards. Every extraction
records where its value came from, so this module takes one of those records
and produces the source itself with the place marked: the page of the scan with
a box drawn around the words the engine read, the cell of the workbook with its
neighbours for context, the captured page with the matched fragment.

That round trip is the difference between saying "the total is 18.392,00" and
showing the pixels it was read from. It needs no new data - the coordinates have
been in the database since ingestion.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass, field
from typing import Any

from iep.api.vocabulary import locator_summary
from iep.domain.enums import MediaKind

# The raster resolution used for PDF pages. It only has to be high enough to
# read on screen: the locator's own coordinates are in PDF points and are
# converted, so the box lands correctly whatever this is set to.
PDF_VIEW_DPI = 150
POINTS_PER_INCH = 72.0


@dataclass
class Box:
    """A rectangle in the coordinate space of the image that is served."""

    left: int
    top: int
    width: int
    height: int


@dataclass
class Cell:
    reference: str
    value: str
    is_target: bool = False
    is_header: bool = False


@dataclass
class EvidenceView:
    kind: str
    summary: str
    # An image is served for scans and PDF pages; the rest render as structure.
    has_image: bool = False
    image_width: int = 0
    image_height: int = 0
    boxes: list[Box] = field(default_factory=list)
    box_note: str = ""
    snippet: str = ""
    rows: list[list[Cell]] = field(default_factory=list)
    sheet: str = ""
    facts: list[tuple[str, str]] = field(default_factory=list)
    inputs: list[uuid.UUID] = field(default_factory=list)
    note: str = ""


def build(
    *,
    locator: dict[str, Any],
    media_kind: str | None,
    data: bytes | None,
) -> EvidenceView:
    """A view model for one locator. `data` is the source document's bytes."""
    kind = str(locator.get("kind"))
    view = EvidenceView(kind=kind, summary=locator_summary(locator))

    if kind == "OCR_WORD_BOX":
        return _ocr(view, locator, media_kind, data)
    if kind == "PDF_PAGE":
        return _pdf(view, locator, data)
    if kind == "EXCEL_CELL":
        return _excel(view, locator, data)
    if kind == "HTML_SELECTOR":
        view.snippet = str(locator.get("snippet") or "")
        view.facts = [
            ("Página capturada", str(locator.get("url") or "")),
            ("Selector CSS", str(locator.get("selector") or "")),
            ("Momento de la captura", str(locator.get("captured_at") or "")),
        ]
        return view
    if kind == "API_FIELD":
        view.facts = [
            ("Endpoint consultado", str(locator.get("endpoint") or "")),
            ("Registro", str(locator.get("record_id") or "")),
            ("Ruta dentro del JSON", str(locator.get("json_path") or "")),
            ("Versión del contrato", str(locator.get("contract_version") or "")),
        ]
        view.snippet = _json_excerpt(data, str(locator.get("record_id") or ""))
        return view
    if kind == "DERIVED":
        raw = locator.get("inputs") or ()
        view.inputs = [uuid.UUID(str(value)) for value in raw]
        view.facts = [("Regla que lo calculó", str(locator.get("rule") or ""))]
        view.note = (
            "Este valor no se leyó de ningún documento: lo calculó el sistema "
            "sumando los valores de abajo. Cada uno de ellos sí tiene su sitio "
            "exacto en un documento."
        )
        return view

    view.note = "No hay visor para este tipo de localizador."
    return view


# --------------------------------------------------------------------------


def _ocr(
    view: EvidenceView,
    locator: dict[str, Any],
    media_kind: str | None,
    data: bytes | None,
) -> EvidenceView:
    """The scan itself, with a box around the words the engine read.

    The engine is handed the image at its own resolution, so the coordinates it
    reports are already in the served image's pixel space and need no scaling.
    """
    confidence = float(locator.get("word_confidence") or 0)
    view.snippet = str(locator.get("snippet") or "")
    view.box_note = f"Confianza del OCR sobre estas palabras: {confidence:.0f} %"
    view.boxes = [
        Box(
            left=int(locator.get("left") or 0),
            top=int(locator.get("top") or 0),
            width=int(locator.get("width") or 0),
            height=int(locator.get("height") or 0),
        )
    ]

    if data is None:
        view.note = "El contenido de este documento no está almacenado."
        return view

    if media_kind in (MediaKind.JPEG, MediaKind.PNG):
        size = _image_size(data)
        if size is None:
            view.note = "No se pudo abrir la imagen."
            return view
        view.has_image, view.image_width, view.image_height = True, size[0], size[1]
        return view

    # A PDF that had to be rasterised for OCR: rebuild the same raster.
    page_index = max(int(locator.get("page") or 1) - 1, 0)
    raster = _render_pdf_page(data, page_index, dpi=PDF_VIEW_DPI)
    if raster is None:
        view.note = "No se pudo rasterizar la página."
        return view
    _, width, height = raster
    view.has_image, view.image_width, view.image_height = True, width, height
    return view


def _pdf(view: EvidenceView, locator: dict[str, Any], data: bytes | None) -> EvidenceView:
    """A rasterised PDF page with the quoted text boxed.

    The locator stores a character span, which is an offset into the extracted
    text and not a position on the page. Searching for the snippet gives the
    rectangles back, which is why the snippet is stored alongside the span.
    """
    view.snippet = str(locator.get("snippet") or "")
    if data is None:
        view.note = "El contenido de este documento no está almacenado."
        return view

    page_index = max(int(locator.get("page") or 1) - 1, 0)
    raster = _render_pdf_page(data, page_index, dpi=PDF_VIEW_DPI)
    if raster is None:
        view.note = "No se pudo rasterizar la página."
        return view
    _, width, height = raster
    view.has_image, view.image_width, view.image_height = True, width, height

    rects = _find_snippet(data, page_index, view.snippet)
    scale = PDF_VIEW_DPI / POINTS_PER_INCH
    view.boxes = [
        Box(
            left=int(rect[0] * scale),
            top=int(rect[1] * scale),
            width=int((rect[2] - rect[0]) * scale),
            height=int((rect[3] - rect[1]) * scale),
        )
        for rect in rects
    ]
    if view.boxes:
        view.box_note = "Texto nativo del PDF: no ha hecho falta OCR."
    else:
        view.box_note = (
            "El texto está en esta página, pero no se ha podido dibujar el recuadro exacto."
        )
    return view


def _excel(view: EvidenceView, locator: dict[str, Any], data: bytes | None) -> EvidenceView:
    """The target cell with the block around it, so the row reads in context."""
    view.sheet = str(locator.get("sheet") or "")
    target = str(locator.get("cell") or "")
    view.facts = [("Hoja", view.sheet), ("Celda", target)]
    if data is None:
        view.note = "El contenido de este documento no está almacenado."
        return view

    try:
        import openpyxl
        from openpyxl.utils import column_index_from_string, get_column_letter

        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        if view.sheet in workbook.sheetnames:
            sheet = workbook[view.sheet]
        else:
            # The locator names a sheet that is no longer there. Falling back to
            # the first one still shows the shape of the data, and the note says
            # the cell could not be placed exactly.
            sheet = workbook[workbook.sheetnames[0]]
            view.note = f"El libro ya no tiene una hoja llamada «{view.sheet}»."
        row_number = int(locator.get("row") or 1)
        column_number = column_index_from_string(str(locator.get("column") or "A"))

        first_row = max(row_number - 3, 1)
        last_row = row_number + 3
        first_column = max(column_number - 3, 1)
        last_column = column_number + 3

        # The header row is what makes a cell reference meaningful, so it is
        # always included even when the target sits far below it.
        row_numbers = [1] if first_row > 1 else []
        row_numbers += list(range(first_row, last_row + 1))

        grid: list[list[Cell]] = []
        for r in row_numbers:
            line = [Cell(reference=str(r), value=str(r), is_header=True)]
            for c in range(first_column, last_column + 1):
                letter = get_column_letter(c)
                raw = sheet.cell(row=r, column=c).value
                line.append(
                    Cell(
                        reference=f"{letter}{r}",
                        value="" if raw is None else str(raw),
                        is_target=(r == row_number and c == column_number),
                    )
                )
            grid.append(line)

        header = [Cell(reference="", value="", is_header=True)]
        header += [
            Cell(reference="", value=get_column_letter(c), is_header=True)
            for c in range(first_column, last_column + 1)
        ]
        view.rows = [header, *grid]
        workbook.close()
    except Exception:
        # A workbook that no longer opens is not worth failing the page over:
        # the locator itself is still shown.
        view.note = "No se pudo abrir el libro para mostrar la celda en contexto."
    return view


# --------------------------------------------------------------------------


def render_page_png(data: bytes, media_kind: str | None, page: int) -> bytes | None:
    """The bytes served to the browser for the image view."""
    if media_kind in (MediaKind.JPEG, MediaKind.PNG):
        return data
    raster = _render_pdf_page(data, max(page - 1, 0), dpi=PDF_VIEW_DPI)
    return raster[0] if raster else None


def _render_pdf_page(data: bytes, page_index: int, *, dpi: int) -> tuple[bytes, int, int] | None:
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as document:
            if page_index >= document.page_count:
                return None
            pixmap = document.load_page(page_index).get_pixmap(dpi=dpi)
            return bytes(pixmap.tobytes("png")), pixmap.width, pixmap.height
    except Exception:
        return None


def _find_snippet(data: bytes, page_index: int, snippet: str) -> list[tuple[float, ...]]:
    text = (snippet or "").strip()
    if not text:
        return []
    try:
        import pymupdf

        with pymupdf.open(stream=data, filetype="pdf") as document:
            if page_index >= document.page_count:
                return []
            page = document.load_page(page_index)
            found = page.search_for(text[:80])
            if not found:
                # Long snippets wrap across lines, which search_for will not
                # match. The first few words are enough to point at the line.
                head = " ".join(text.split()[:4])
                found = page.search_for(head) if head else []
            return [tuple(rect) for rect in found[:6]]
    except Exception:
        return []


def _image_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return image.width, image.height
    except Exception:
        return None


def _json_excerpt(data: bytes | None, record_id: str) -> str:
    """The part of a stored API response that concerns one record."""
    if data is None:
        return ""
    try:
        import json

        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return ""

    def walk(node: Any) -> Any | None:
        if isinstance(node, dict):
            if record_id and record_id in node.values():
                return node
            for value in node.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    import json

    match = walk(payload)
    return json.dumps(match or payload, indent=2, ensure_ascii=False)[:4000]
