"""Rendering the filed report here instead of in the reader's print dialogue.

The report was always printable, and that was the problem: the artefact came
out of whatever the reviewer's browser was set to do. One such PDF arrived as
26 bitmaps, 2 MB, zero embedded fonts and zero selectable text - Chrome's
"print as image" - so the letters were ragged, nothing could be searched, and
scrolling it was slow. Rendered here from the same stored HTML it is 0.6 MB
with ten embedded fonts and 11484 text operators.

No browser runs in these tests. What is tested is the contract with it: a
missing renderer is a configuration fact and not a crash, a renderer that
produces nothing is an error and not an empty file, and the bytes handed to
it are the stored report's own.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest

from iep.reporting import pdf


class TestFindingARenderer:
    def test_it_answers_with_a_path_or_with_nothing(self) -> None:
        """Never raises: a host without Chromium has one endpoint fewer, and
        the screen says so rather than failing to be interpreted."""
        found = pdf.renderer()
        assert found is None or Path(found).name

    def test_the_candidates_cover_the_usual_names(self) -> None:
        """Debian calls it `chromium`, which is what the image installs;
        a developer's host may have Chrome under any of the others."""
        assert "chromium" in pdf.CANDIDATES
        assert any("chrome" in name for name in pdf.CANDIDATES)


class TestWhenThereIsNoRenderer:
    def test_it_says_so_rather_than_failing_obscurely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pdf, "renderer", lambda: None)
        with pytest.raises(pdf.PdfUnavailableError) as raised:
            pdf.render(b"<p>hola</p>")
        # The message names what is missing and where it normally comes from.
        assert "chromium" in str(raised.value).lower()

    def test_that_is_not_the_same_error_as_a_failed_render(self) -> None:
        """One is "this machine cannot", the other is "it tried and did not" -
        and the API answers them differently."""
        assert not issubclass(pdf.PdfUnavailableError, pdf.PdfRenderError)
        assert not issubclass(pdf.PdfRenderError, pdf.PdfUnavailableError)


class TestWhenTheRendererProducesNothing:
    def test_an_empty_result_is_an_error_and_not_an_empty_pdf(self) -> None:
        """A zero-byte file served as `application/pdf` is the worst outcome:
        the caller gets a document that is not one. `sys.executable` stands in
        for a renderer here - it is a real binary that will not understand
        these flags, which is exactly the failure being tested.
        """
        with pytest.raises(pdf.PdfRenderError):
            pdf.render(b"<p>hola</p>", binary=sys.executable, timeout=30.0)

    def test_a_binary_that_cannot_be_launched_is_reported(self) -> None:
        with pytest.raises(pdf.PdfRenderError):
            pdf.render(b"<p>hola</p>", binary="/definitivamente/no/existe", timeout=5.0)


class TestWhatTheRendererIsGiven:
    def test_the_stored_html_and_a_file_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The report is already on disk, so it is opened as a file rather
        than fetched over HTTP: no network, and a container that cannot reach
        its own API still produces the document.

        The URI is turned back into a path with `url2pathname` rather than by
        stripping `file:///`. Stripping three slashes leaves an absolute path
        on Windows (`C:/...`) and a relative one on Linux (`tmp/...`), so the
        first version of this test passed here and failed on the runner.
        """
        seen: dict[str, object] = {}

        class Result:
            stderr = ""
            stdout = ""

        def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
            seen["command"] = list(command)
            target = next(
                arg.split("=", 1)[1] for arg in command if arg.startswith("--print-to-pdf=")
            )
            source = Path(url2pathname(urlparse(command[-1]).path))
            seen["html"] = source.read_bytes()
            Path(target).write_bytes(b"%PDF-1.4 fake")
            return Result()

        monkeypatch.setattr(pdf.subprocess, "run", fake_run)
        out = pdf.render(b"<p>el informe</p>", binary="/usr/bin/chromium")

        assert out == b"%PDF-1.4 fake"
        assert seen["html"] == b"<p>el informe</p>"
        command = seen["command"]
        assert isinstance(command, list)
        assert command[0] == "/usr/bin/chromium"
        assert "--headless" in command
        # A browser footer would stamp a localhost URL across a filed document.
        assert "--no-pdf-header-footer" in command
        assert str(command[-1]).startswith("file://")

    def test_nothing_is_left_behind(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The HTML and the PDF are written to a temporary directory that goes
        away: a report is evidence and does not belong in /tmp afterwards."""
        paths: list[Path] = []

        class Result:
            stderr = ""
            stdout = ""

        def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
            target = next(
                arg.split("=", 1)[1] for arg in command if arg.startswith("--print-to-pdf=")
            )
            paths.append(Path(target).parent)
            Path(target).write_bytes(b"%PDF-1.4 fake")
            return Result()

        monkeypatch.setattr(pdf.subprocess, "run", fake_run)
        pdf.render(b"<p>x</p>", binary="/usr/bin/chromium")

        assert paths and not paths[0].exists()
