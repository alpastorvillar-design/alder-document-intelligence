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
                raise ExtractionError("el PDF no tiene páginas", retryable=False)
            return PdfPages([doc.load_page(i).get_text("text") for i in range(doc.page_count)])
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(
            f"PDF could not be read: {type(exc).__name__}", retryable=False
        ) from exc


def _split_on_a_boundary(block: str, max_chars: int) -> list[tuple[int, str]]:
    """Cut `block` into pieces at line or word boundaries, never mid-word.

    Slicing every `max_chars` characters is simpler and was what this did, but
    it cut wherever the count landed. A report whose section heading fell
    across a boundary was indexed as `...Gastos de personal` followed by
    ` declarados: 77.604,00 EUR`, and a search for the heading matched neither
    piece - the phrase existed in the document and was unfindable.

    A phrase can still straddle two pieces when it crosses the boundary line,
    which is why retrieval is measured against fixed probes with known answers
    rather than assumed to work. Cutting on a boundary removes the common case
    without duplicating text or blurring the character span each piece records.
    """
    pieces: list[tuple[int, str]] = []
    offset = 0
    while offset < len(block):
        remaining = len(block) - offset
        if remaining <= max_chars:
            pieces.append((offset, block[offset:]))
            break
        window = block[offset : offset + max_chars]
        cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            # One unbroken run longer than the window: there is no boundary to
            # prefer, so fall back to the hard cut rather than loop forever.
            cut = max_chars
        pieces.append((offset, block[offset : offset + cut]))
        offset += cut
        # Step over the whitespace the cut landed on so the next piece starts
        # on a character. The offset stays exact, so the locator does too.
        while offset < len(block) and block[offset] in " \n":
            offset += 1
    return pieces


def chunk_pages(pages: PdfPages, *, max_chars: int = 900) -> tuple[TextChunk, ...]:
    """Split into paragraph-sized chunks, each keeping its page and offsets.

    Every chunk's `char_start`/`char_end` indexes the page text it came from,
    so `page_text[char_start:char_end]` is the chunk verbatim. A test asserts
    that, because a locator that is off by the length of a stripped newline
    points a reviewer at the wrong place while looking entirely plausible.
    """
    chunks: list[TextChunk] = []
    ordinal = 0
    for page_number, page_text in enumerate(pages.pages, start=1):
        cursor = 0
        for raw_block in page_text.split("\n\n"):
            block = raw_block.strip()
            if not block:
                cursor += len(raw_block) + 2
                continue
            # Locate the stripped block, not the raw one: searching for the
            # raw block and then stripping it shifted every offset in the
            # block by however much leading whitespace had been removed.
            start = page_text.find(block, cursor)
            if start < 0:
                start = cursor
            cursor = start + len(block)
            for offset, piece in _split_on_a_boundary(block, max_chars):
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
