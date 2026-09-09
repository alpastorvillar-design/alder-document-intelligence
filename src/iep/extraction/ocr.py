"""OCR via a local Tesseract installation.

`image_to_data` is used rather than `image_to_string` because the per-word
confidence and bounding box are the whole point: a value read off a scan is
only usable if the reviewer can be shown where on the page it was read from and
how sure the engine was. The mean word confidence also decides whether the
document is trusted or sent to review.

No cloud OCR, no downloaded model: Tesseract with the Spanish language pack is
installed in the image, so results are reproducible from the Dockerfile alone.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pytesseract
from PIL import Image, ImageOps

from iep.domain.contracts import OcrWordBoxLocator
from iep.extraction.base import ExtractionError, TextChunk

EXTRACTOR_VERSION = "ocr-tesseract/1.0.0"

# Tesseract reports -1 for entries that are layout, not text.
_NO_CONFIDENCE = -1.0


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int
    line_id: tuple[int, int, int]

    def locator(self, page: int) -> OcrWordBoxLocator:
        return OcrWordBoxLocator(
            page=page,
            left=self.left,
            top=self.top,
            width=self.width,
            height=self.height,
            word_confidence=self.confidence,
            snippet=self.text[:500],
        )


@dataclass(frozen=True)
class OcrLine:
    text: str
    words: tuple[OcrWord, ...]

    @property
    def confidence(self) -> float:
        scored = [w.confidence for w in self.words if w.confidence >= 0]
        return sum(scored) / len(scored) if scored else 0.0

    def locator(self, page: int) -> OcrWordBoxLocator:
        """A box covering the whole line, carrying the line's confidence."""
        left = min(w.left for w in self.words)
        top = min(w.top for w in self.words)
        right = max(w.left + w.width for w in self.words)
        bottom = max(w.top + w.height for w in self.words)
        return OcrWordBoxLocator(
            page=page,
            left=left,
            top=top,
            width=right - left,
            height=bottom - top,
            word_confidence=self.confidence,
            snippet=self.text[:500],
        )


@dataclass(frozen=True)
class OcrPage:
    page: int
    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_confidence(self) -> float:
        words = [w for line in self.lines for w in line.words if w.confidence >= 0]
        if not words:
            return 0.0
        return sum(w.confidence for w in words) / len(words)


def tesseract_version() -> str:
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception as exc:
        raise ExtractionError(f"Tesseract is not available: {exc}", retryable=False) from exc


def recognise(
    image_bytes: bytes, *, page: int, language: str, timeout_seconds: float = 30.0
) -> OcrPage:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception as exc:
        raise ExtractionError(f"image could not be opened: {type(exc).__name__}") from exc

    # Greyscale and autocontrast before recognition: it costs nothing and
    # measurably helps on the low-quality scans in the corpus.
    prepared = ImageOps.autocontrast(image.convert("L"))

    try:
        data = pytesseract.image_to_data(
            prepared,
            lang=language,
            output_type=pytesseract.Output.DICT,
            timeout=timeout_seconds,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise ExtractionError("Tesseract binary not found", retryable=False) from exc
    except RuntimeError as exc:
        # pytesseract raises RuntimeError when its subprocess exceeds timeout.
        raise ExtractionError("OCR timed out", retryable=True) from exc
    except Exception as exc:
        # A transient failure here (a killed subprocess, a temp-file problem)
        # is worth retrying; a malformed image is not, and was caught above.
        raise ExtractionError(f"OCR failed: {type(exc).__name__}", retryable=True) from exc

    words_by_line: dict[tuple[int, int, int], list[OcrWord]] = {}
    for index, raw_text in enumerate(data["text"]):
        text = (raw_text or "").strip()
        if not text:
            continue
        try:
            confidence = float(data["conf"][index])
        except (TypeError, ValueError):
            confidence = _NO_CONFIDENCE
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        words_by_line.setdefault(key, []).append(
            OcrWord(
                text=text,
                confidence=confidence,
                left=int(data["left"][index]),
                top=int(data["top"][index]),
                width=int(data["width"][index]),
                height=int(data["height"][index]),
                line_id=key,
            )
        )

    lines = tuple(
        OcrLine(text=" ".join(w.text for w in words), words=tuple(words))
        for _, words in sorted(words_by_line.items())
        if words
    )
    return OcrPage(page=page, lines=lines)


def chunks_from_pages(pages: list[OcrPage]) -> tuple[TextChunk, ...]:
    chunks: list[TextChunk] = []
    ordinal = 0
    for page in pages:
        for line in page.lines:
            if len(line.text) < 3:
                continue
            chunks.append(
                TextChunk(ordinal=ordinal, text=line.text, locator=line.locator(page.page))
            )
            ordinal += 1
    return tuple(chunks)


def mean_confidence(pages: list[OcrPage]) -> float:
    """Mean per-word confidence across pages, on Tesseract's 0-100 scale."""
    words = [w for page in pages for line in page.lines for w in line.words if w.confidence >= 0]
    if not words:
        return 0.0
    return sum(w.confidence for w in words) / len(words)
