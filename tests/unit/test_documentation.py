"""Every relative link in the documentation resolves.

Written after one did not. `docs/es/rag.md` pointed at `img/06-ask.png`, which
is where the image is relative to `docs/` and not relative to `docs/es/`, so
the Spanish page showed a broken image while the English one beside it was
fine. Nothing fails when this happens - the page renders, with a hole in it -
and the bilingual pairing is what makes it easy: a path that is correct in one
language is one directory wrong in the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# `docs/` and `README.es.md` are not copied into the runtime image; the suite
# also runs from /app inside it.
SKIP = not (REPO / "docs").exists()

# Anything that is not a relative path into the repository: absolute URLs, mail
# links, and in-page anchors.
EXTERNAL = re.compile(r"^(https?:|mailto:|#)")
LINK = re.compile(r"]\(([^)\s]+)")


def markdown_files() -> list[Path]:
    skipped = {".venv", ".review-venv", "node_modules", "var", "_control", ".git"}
    return sorted(
        path
        for path in REPO.rglob("*.md")
        if not skipped.intersection(path.relative_to(REPO).parts)
    )


@pytest.mark.skipif(SKIP, reason="the documentation is not part of the runtime image")
def test_every_relative_link_resolves() -> None:
    broken: list[str] = []
    for path in markdown_files():
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            if EXTERNAL.match(target):
                continue
            # A link may carry an anchor; only the path part is a file.
            relative = target.split("#")[0]
            if not relative:
                continue
            if not (path.parent / relative).exists():
                broken.append(f"{path.relative_to(REPO).as_posix()} -> {target}")
    assert not broken, "links that go nowhere:\n  " + "\n  ".join(broken)


@pytest.mark.skipif(SKIP, reason="the documentation is not part of the runtime image")
def test_every_screenshot_is_used_and_present() -> None:
    """The images and the pages that show them stay in step.

    An unreferenced screenshot is usually one that was replaced under a new
    name, leaving the old file in the repository and the page still pointing at
    whichever of the two nobody meant.
    """
    images = {path.name for path in (REPO / "docs" / "img").glob("*.png")}
    referenced = {
        Path(target).name
        for path in markdown_files()
        for target in LINK.findall(path.read_text(encoding="utf-8"))
        if target.endswith(".png")
    }
    assert not images - referenced, f"screenshots nothing shows: {sorted(images - referenced)}"
    assert not referenced - images, f"shown but not in docs/img: {sorted(referenced - images)}"
