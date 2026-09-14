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


def test_shot_plan_resolves_ids_from_the_current_demo_run() -> None:
    module = runpy.run_path(str(SCRIPT))

    responses = {
        "/dossiers?reference=INN-2025-042": [{"id": "current-dossier-42"}],
        "/dossiers?reference=INN-2025-041": [{"id": "current-dossier-41"}],
        "/dossiers/current-dossier-41/documents": [
            {
                "id": "current-scan",
                "original_filename": "justificante-02-FS-2025-0588.jpg",
            }
        ],
        "/dossiers/current-dossier-41/extractions": [
            {
                "id": "current-evidence",
                "document_id": "current-scan",
                "field_path": "invoice.total_eur",
            }
        ],
    }

    def current_demo_response(path: str) -> object:
        return responses[path]

    module["shot_plan"].__globals__["api_json"] = current_demo_response
    plan = {name: url for name, url, _height, _keep in module["shot_plan"]()}

    assert plan["03-review"].endswith("/ui/dossiers/current-dossier-42")
    assert plan["04-evidence"].endswith("/ui/evidence/current-evidence")
    assert plan["05-report"].endswith("/dossiers/current-dossier-42/reports/latest.html")
