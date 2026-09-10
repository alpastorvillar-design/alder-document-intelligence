"""The generated corpus is a stable measurement input, not a moving target."""

from __future__ import annotations

import hashlib
from pathlib import Path

from corpus.generate import write_corpus


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_two_independent_corpus_builds_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_corpus(first)
    write_corpus(second)
    assert _tree_hashes(first) == _tree_hashes(second)


def test_no_generated_page_runs_past_its_margin() -> None:
    """Text drawn past the page edge is silently clipped, not reported.

    PyMuPDF's `insert_text` places a line wherever it is told and never
    complains, so a line that grew too long stays in the text layer - and
    therefore keeps extracting correctly - while being cut off on screen. The
    evidence viewer shows these pages to a reviewer, so a clipped line is a
    visible defect in the product even though every field still reads.
    """
    import pymupdf
    from corpus.dataset import DOSSIERS
    from corpus.documents import invoice_pdf, technical_report
    from corpus.render import A4, MARGIN_X

    limit = A4.width - MARGIN_X
    overflowing: list[str] = []
    for spec in DOSSIERS:
        pages = [("memoria", technical_report(spec))]
        pages += [
            (f"factura {invoice.invoice_number}", invoice_pdf(invoice, reference=spec.reference))
            for invoice in spec.invoices
        ]
        for name, data in pages:
            with pymupdf.open(stream=data, filetype="pdf") as document:
                for page in document:
                    for block in page.get_text("dict")["blocks"]:
                        for line in block.get("lines", []):
                            if line["bbox"][2] > limit:
                                text = "".join(span["text"] for span in line["spans"])
                                overflowing.append(f"{spec.reference} {name}: {text[:70]}")

    assert not overflowing, "lines drawn past the right margin: " + "; ".join(overflowing)
