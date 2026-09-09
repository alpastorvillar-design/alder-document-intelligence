"""Rendering primitives for the synthetic corpus.

Every document is built with PyMuPDF's built-in base-14 fonts and, for the
scanned receipts, rasterised from that same PDF before being degraded. Nothing
depends on a font file being installed, so a document generated on a developer
machine and one generated in CI are byte-comparable inputs to the extractors.
"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass

import pymupdf
from PIL import Image, ImageFilter

A4 = pymupdf.paper_rect("a4")
MARGIN_X = 56.0
TOP_Y = 72.0
BOTTOM_Y = A4.height - 60.0


@dataclass
class Line:
    text: str
    size: float = 10.5
    bold: bool = False
    space_after: float = 4.0


def _font(bold: bool) -> str:
    return "hebo" if bold else "helv"


def build_pdf(pages: list[list[Line]], *, title: str) -> bytes:
    """Lay out pre-split pages of text and return the PDF bytes."""
    doc = pymupdf.open()
    # A fixed creation date keeps generation deterministic: without it two
    # renders of the same content differ in bytes and stop being duplicates.
    doc.set_metadata(
        {
            "title": title,
            "producer": "innovation-evidence-pipeline corpus",
            "creationDate": "D:20250101000000Z",
            "modDate": "D:20250101000000Z",
        }
    )
    for lines in pages:
        page = doc.new_page(width=A4.width, height=A4.height)
        y = TOP_Y
        for line in lines:
            if y > BOTTOM_Y:
                break
            page.insert_text(
                (MARGIN_X, y),
                line.text,
                fontname=_font(line.bold),
                fontsize=line.size,
            )
            y += line.size + line.space_after
    # PyMuPDF otherwise writes a fresh trailer identifier on each save even
    # when metadata and page contents are identical.
    data: bytes = doc.tobytes(reproducible=True, no_new_id=True)
    doc.close()
    return data


def paginate(lines: list[Line], *, first_page_offset: float = 0.0) -> list[list[Line]]:
    """Split a flat line list into pages that fit the A4 text area."""
    pages: list[list[Line]] = []
    current: list[Line] = []
    y = TOP_Y + first_page_offset
    for line in lines:
        advance = line.size + line.space_after
        if y + advance > BOTTOM_Y and current:
            pages.append(current)
            current = []
            y = TOP_Y
        current.append(line)
        y += advance
    if current:
        pages.append(current)
    return pages or [[]]


def wrap(text: str, width: int = 96) -> list[str]:
    words = text.split()
    out: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if len(candidate) > width and line:
            out.append(line)
            line = word
        else:
            line = candidate
    if line:
        out.append(line)
    return out


def rasterise_and_degrade(
    pdf_bytes: bytes,
    *,
    dpi: int = 200,
    blur: float,
    noise: int,
    rotation: float,
    jpeg_quality: int,
    seed: int,
) -> bytes:
    """Turn a clean PDF page into something that looks scanned.

    Blur, sensor noise, a small skew and JPEG loss are applied in that order,
    which is roughly the order a real office scanner introduces them. The seed
    makes the degradation deterministic so OCR accuracy is measured against a
    fixed input rather than a new one on every run.
    """
    rng = random.Random(seed)
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        pixmap = doc.load_page(0).get_pixmap(dpi=dpi)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    image = image.convert("L")
    if blur > 0:
        image = image.filter(ImageFilter.GaussianBlur(radius=blur))

    if noise > 0:
        pixels = image.load()
        assert pixels is not None
        width, height = image.size
        # Sparse salt-and-pepper plus a mild global grain: enough to move
        # Tesseract's per-word confidence without destroying the page.
        for _ in range((width * height) // max(400 - noise * 4, 40)):
            x = rng.randrange(width)
            y = rng.randrange(height)
            pixels[x, y] = rng.randrange(0, 90) if rng.random() < 0.5 else rng.randrange(170, 255)

    if rotation:
        image = image.rotate(rotation, resample=Image.Resampling.BICUBIC, fillcolor=255)

    buffer = io.BytesIO()
    if jpeg_quality >= 90:
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="JPEG", quality=jpeg_quality)
    return buffer.getvalue()
