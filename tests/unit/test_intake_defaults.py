"""The intake form suggests a call page the worker can actually capture.

The form used to arrive with `http://devsources:8080/...` written into it. That
name is only resolvable, and only allowlisted, inside Compose. With the API and
the worker on the host the scraper refused it, the dossier got an
EXTERNAL_SOURCE_UNAVAILABLE blocker, and nothing submitted from the form as it
arrived could ever be approved.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from iep.api.routes.ui import default_call_page_url
from iep.config import Settings

COMPOSE = Settings(
    registry_api_base_url="http://devsources:8080",
    scraper_allowlist="devsources,localhost,127.0.0.1",
)
# What scripts/dev.ps1 -Mode host configures: the same simulator, through the
# port Compose publishes, with the default allowlist.
HOST = Settings(
    registry_api_base_url="http://127.0.0.1:8080",
    scraper_allowlist="localhost,127.0.0.1",
)


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (COMPOSE, "http://devsources:8080/public/convocatoria.html"),
        (HOST, "http://127.0.0.1:8080/public/convocatoria.html"),
    ],
    ids=["compose", "host"],
)
def test_the_suggested_call_page_follows_the_deployment(settings: Settings, expected: str) -> None:
    url = default_call_page_url(settings)
    assert url == expected
    assert urlparse(url).hostname in settings.scraper_allowed_hosts


def test_the_address_the_form_used_to_carry_is_refused_on_the_host() -> None:
    """Why a fixed address in the template was a defect and not a default."""
    assert "devsources" not in HOST.scraper_allowed_hosts


def test_a_trailing_slash_in_the_configuration_does_not_double_up() -> None:
    settings = Settings(registry_api_base_url="http://127.0.0.1:8080/")
    assert default_call_page_url(settings) == "http://127.0.0.1:8080/public/convocatoria.html"
