"""The screenshot publisher must not turn an HTTP error into documentation."""

from __future__ import annotations

import runpy
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "shots.py"


def test_http_error_stops_before_chrome_can_replace_the_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = runpy.run_path(str(SCRIPT))

    def unavailable(*args: object, **kwargs: object) -> None:
        raise urllib.error.HTTPError(
            url="http://127.0.0.1:8000/missing",
            code=404,
            msg="Not found",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("Chrome must not run after a failed preflight"),
    )

    with pytest.raises(urllib.error.HTTPError, match="HTTP Error 404"):
        module["shoot"]("missing", "http://127.0.0.1:8000/missing", 800, None)
