"""Render the justification report to PDF here, not in the reader's browser.

The report has always been printable: `@media print` sizes it for A4, repeats
table headings across pages and forces the light palette. What it relied on
was the reviewer pressing Ctrl+P and choosing "Save as PDF", and that turned
out to put the archived artefact at the mercy of their print dialogue. One
such file arrived as 26 bitmaps, 2 MB, zero embedded fonts and zero
selectable text - Chrome's "print as image" - so the letters were ragged, the
borders soft, nothing could be searched or copied, and scrolling it was slow.
The same report rendered through `--print-to-pdf` is 0.52 MB with seven
embedded fonts and 9562 text operators.

So it is rendered here, with the engine the stylesheet was written and
reviewed against. A paged-media library would be the more elegant answer and
was the first choice, until the CSS was checked against it: the report uses
`color-mix()` for every state tag and flexbox for the masthead, the summary
row and the pills, and WeasyPrint supports neither the first nor all of the
second. Retargeting a reviewed document to a weaker engine trades a known
result for an unknown one.

What this does not claim: byte-identical output. Chromium stamps a creation
date into the file, so two renderings of one report differ in those bytes.
The *hashed* artefact is the HTML, which is what `reports` stores and what the
audit trail names; the PDF is a rendering of it, and the response carries that
hash so a reader can tell which report a given PDF came from.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# Debian names the binary `chromium`; the image installs that package. The
# others are here so a developer running the API on a host with Chrome or a
# differently packaged Chromium gets the same endpoint rather than a 503.
CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)

# A cold Chromium on a loaded machine takes a few seconds. Past this something
# is wrong and a request should say so rather than hold a worker open.
TIMEOUT_SECONDS = 60.0


class PdfUnavailableError(RuntimeError):
    """No renderer on this machine, which is a configuration fact."""


class PdfRenderError(RuntimeError):
    """The renderer ran and did not produce a file."""


def renderer() -> str | None:
    """Path to a usable Chromium, or `None`."""
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def render(html: bytes, *, binary: str | None = None, timeout: float = TIMEOUT_SECONDS) -> bytes:
    """The report as a PDF, rendered from its own bytes.

    The HTML is written to a file and opened as `file://` rather than fetched
    over HTTP: the artefact is already stored, the renderer needs no network,
    and a container that cannot reach its own API still produces the document.
    """
    engine = binary or renderer()
    if engine is None:
        raise PdfUnavailableError(
            "No hay ningún Chromium en este proceso para generar el PDF. "
            "La imagen lo trae instalado; en un host hace falta `chromium` "
            "o `google-chrome` en el PATH."
        )

    with tempfile.TemporaryDirectory(prefix="iep-pdf-") as workspace:
        source = Path(workspace) / "report.html"
        target = Path(workspace) / "report.pdf"
        source.write_bytes(html)
        command = [
            engine,
            "--headless",
            "--disable-gpu",
            # No sandbox: the process already runs unprivileged in a
            # container, and the sandbox needs privileges it does not have.
            # The input is a file this process just wrote from its own
            # database - not a page from the internet.
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # The report's own header is the identification block; a browser
            # footer would stamp a localhost URL across the bottom of a filed
            # document.
            "--no-pdf-header-footer",
            f"--print-to-pdf={target}",
            source.as_uri(),
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PdfRenderError(f"El renderizador no terminó en {timeout:.0f} segundos.") from exc
        except OSError as exc:
            raise PdfRenderError(f"No se pudo lanzar el renderizador: {exc}") from exc

        if not target.exists() or target.stat().st_size == 0:
            detail = (result.stderr or result.stdout or "").strip()[-400:]
            raise PdfRenderError(f"El renderizador no produjo ningún PDF. {detail}".strip())
        return target.read_bytes()
