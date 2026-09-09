"""Native PDF text extraction.

A PDF that already carries a text layer is read directly. Running OCR over it
would be slower, less accurate and would throw away exact character offsets, so
the pipeline only falls back to OCR when this returns too little to work with.
"""

from __future__ import annotations

import pymupdf

from iep.domain.contracts import PdfPageLocator
from iep.extraction.base import ExtractionError, TextChunk

EXTRACTOR_VERSION = "pdf-text/1.0.0"

# Below this many characters per page a "text" PDF is really a scan with a
# stray label on it, and the OCR path gives a better answer.
MIN_CHARS_PER_PAGE = 120


class PdfPages:
    def __init__(self, pages: list[str]) -> None:
        self.pages = pages

    @property
    def text(self) -> str:
        return "\n".join(self.pages)

    @property
    def total_chars(self) -> int:
        return sum(len(p.strip()) for p in self.pages)

    def has_usable_text_layer(self) -> bool:
        if not self.pages:
            return False
        return self.total_chars >= MIN_CHARS_PER_PAGE * min(len(self.pages), 3)

    def locate(self, needle: str) -> PdfPageLocator | None:
        """Find `needle` and return where it is, page and character offsets."""
        for index, page_text in enumerate(self.pages, start=1):
            position = page_text.find(needle)
            if position >= 0:
                return PdfPageLocator(
                    page=index,
                    char_start=position,
                    char_end=position + len(needle),
                    snippet=needle[:500],
                )
        return None


def read_pages(data: bytes) -> PdfPages:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ExtractionError("PDF is encrypted", retryable=False)
            if doc.page_count == 0:
                # PyMuPDF opens a truncated file without complaining and
                # reports no pages; that is a broken document, not an empty one.
                raise ExtractionError("PDF has no pages", retryable=False)
            return PdfPages([doc.load_page(i).get_text("text") for i in range(doc.page_count)])
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            f"PDF could not be read: {type(exc).__name__}", retryable=False
        ) from exc


def chunk_pages(pages: PdfPages, *, max_chars: int = 900) -> tuple[TextChunk, ...]:
    """Split into paragraph-sized chunks, each keeping its page and offsets."""
    chunks: list[TextChunk] = []
    ordinal = 0
    for page_number, page_text in enumerate(pages.pages, start=1):
        cursor = 0
        for block in page_text.split("\n\n"):
            start = page_text.find(block, cursor)
            if start < 0:
                start = cursor
            cursor = start + len(block)
            block = block.strip()
            if not block:
                continue
            for offset in range(0, len(block), max_chars):
                piece = block[offset : offset + max_chars]
                chunks.append(
                    TextChunk(
                        ordinal=ordinal,
                        text=piece,
                        locator=PdfPageLocator(
                            page=page_number,
                            char_start=start + offset,
                            char_end=start + offset + len(piece),
                            snippet=piece[:200],
                        ),
                    )
                )
                ordinal += 1
    return tuple(chunks)


def render_page_png(
    data: bytes, page_number: int, *, dpi: int, max_pixels: int = 40_000_000
) -> bytes:
    """Rasterise one page so the OCR path can read a scanned PDF."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        page = doc.load_page(page_number - 1)
        scale = dpi / 72.0
        pixels = int(page.rect.width * scale) * int(page.rect.height * scale)
        if pixels > max_pixels:
            raise ExtractionError(
                f"PDF page raster would exceed the {max_pixels} pixel limit",
                retryable=False,
            )
        pixmap = page.get_pixmap(dpi=dpi)
        png: bytes = pixmap.tobytes("png")
        return png


def page_count(data: bytes) -> int:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return int(doc.page_count)
