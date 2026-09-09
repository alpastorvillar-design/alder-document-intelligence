"""Connector and scraper behaviour under failure.

Both are driven through an httpx MockTransport rather than a live service: the
interesting cases are pagination, backoff, contract drift, allowlists and size
limits, all of which are easier to provoke deterministically here than against
the development simulator.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from iep.config import Settings
from iep.connectors import public_page
from iep.connectors.public_page import (
    PublicPageScraper,
    ScraperError,
    StructureChangedError,
)
from iep.connectors.registry import (
    MAX_PAGES,
    ConnectorError,
    RegistryConnector,
    RegistryPerson,
)

PEOPLE = [
    {
        "employee_id": f"EMP-{i:04d}",
        "full_name": f"Person {i}",
        "role": "Engineer",
        "hourly_rate_eur": "30.00",
        "contract_start": "2024-01-01",
        "contract_end": None,
    }
    for i in range(1, 8)
]


def settings(**overrides: Any) -> Settings:
    base = {
        "registry_api_base_url": "http://registry.test",
        "registry_api_page_size": 3,
        "registry_api_max_attempts": 3,
        "registry_api_timeout_seconds": 1.0,
        "scraper_allowlist": "pages.test,localhost",
        "scraper_max_bytes": 4096,
        "scraper_min_interval_seconds": 0.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def page_payload(page: int, size: int, *, contract: str = "registry/v1") -> dict[str, Any]:
    start = (page - 1) * size
    window = PEOPLE[start : start + size]
    return {
        "contract_version": contract,
        "page": page,
        "page_size": size,
        "total": len(PEOPLE),
        "has_more": start + size < len(PEOPLE),
        "items": window,
    }


def registry_client(handler: Any) -> httpx.Client:
    return httpx.Client(base_url="http://registry.test", transport=httpx.MockTransport(handler))


class TestRegistryConnector:
    def test_it_follows_pagination_to_the_end(self) -> None:
        seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            seen.append(page)
            return httpx.Response(200, json=page_payload(page, 3))

        snapshot = RegistryConnector(settings(), client=registry_client(handler)).fetch_personnel()
        assert seen == [1, 2, 3]
        assert len(snapshot.people) == len(PEOPLE)
        assert isinstance(snapshot.people[0], RegistryPerson)

    def test_the_bearer_token_is_sent_and_never_in_the_url(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=page_payload(1, 10))

        RegistryConnector(
            settings(registry_api_token="s3cret", registry_api_page_size=10),
            client=registry_client(handler),
        ).fetch_personnel()
        assert captured[0].headers["Authorization"] == "Bearer s3cret"
        assert "s3cret" not in str(captured[0].url)

    def test_a_5xx_is_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, json=page_payload(1, 10))

        snapshot = RegistryConnector(
            settings(registry_api_page_size=10), client=registry_client(handler)
        ).fetch_personnel()
        assert attempts["n"] == 2
        assert snapshot.attempts == 2

    def test_a_429_is_retried_and_retry_after_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(
            "iep.connectors.registry.time.sleep", lambda seconds: slept.append(seconds)
        )
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json=page_payload(1, 10))

        RegistryConnector(
            settings(registry_api_page_size=10), client=registry_client(handler)
        ).fetch_personnel()
        assert slept == [2.0]

    def test_a_4xx_is_not_retried(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(401)

        with pytest.raises(ConnectorError) as excinfo:
            RegistryConnector(settings(), client=registry_client(handler)).fetch_personnel()
        assert excinfo.value.retryable is False
        assert attempts["n"] == 1

    def test_exhausted_retries_raise_as_retryable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("iep.connectors.registry.time.sleep", lambda seconds: None)
        with pytest.raises(ConnectorError) as excinfo:
            RegistryConnector(
                settings(), client=registry_client(lambda request: httpx.Response(500))
            ).fetch_personnel()
        assert excinfo.value.retryable is True

    def test_an_unknown_contract_version_is_refused(self) -> None:
        handler = lambda request: httpx.Response(  # noqa: E731
            200, json=page_payload(1, 10, contract="registry/v2")
        )
        with pytest.raises(ConnectorError, match="registry speaks"):
            RegistryConnector(
                settings(registry_api_page_size=10), client=registry_client(handler)
            ).fetch_personnel()

    def test_a_response_that_fails_the_contract_is_refused(self) -> None:
        handler = lambda request: httpx.Response(  # noqa: E731
            200, json={"contract_version": "registry/v1", "items": "not a list"}
        )
        with pytest.raises(ConnectorError, match="contract validation"):
            RegistryConnector(settings(), client=registry_client(handler)).fetch_personnel()

    def test_a_non_json_body_is_refused(self) -> None:
        handler = lambda request: httpx.Response(200, content=b"<html>nope</html>")  # noqa: E731
        with pytest.raises(ConnectorError, match="non-JSON"):
            RegistryConnector(settings(), client=registry_client(handler)).fetch_personnel()

    def test_endless_pagination_is_bounded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            payload = page_payload(1, 1)
            payload["has_more"] = True
            payload["page"] = int(request.url.params["page"])
            return httpx.Response(200, json=payload)

        with pytest.raises(ConnectorError, match=f"past {MAX_PAGES}"):
            RegistryConnector(
                settings(registry_api_page_size=1), client=registry_client(handler)
            ).fetch_personnel()

    def test_the_snapshot_hashes_the_same_for_unchanged_data(self) -> None:
        handler = lambda request: httpx.Response(200, json=page_payload(1, 10))  # noqa: E731
        first = RegistryConnector(
            settings(registry_api_page_size=10), client=registry_client(handler)
        ).fetch_personnel()
        second = RegistryConnector(
            settings(registry_api_page_size=10), client=registry_client(handler)
        ).fetch_personnel()
        assert first.raw_payload == second.raw_payload
        assert json.loads(first.raw_payload)["contract_version"] == "registry/v1"

    def test_contract_dates_are_used_for_coverage(self) -> None:
        person = RegistryPerson(
            employee_id="EMP-1",
            full_name="P",
            role="R",
            hourly_rate_eur=Decimal("10"),
            contract_start=date(2024, 1, 1),
            contract_end=date(2025, 6, 30),
        )
        assert person.covers(date(2025, 1, 1))
        assert not person.covers(date(2023, 12, 31))
        assert not person.covers(date(2025, 7, 1))


GOOD_PAGE = """<html><body><dl>
<dd data-field="call-code">CALL-SYN-2025-A</dd>
<dd data-field="eligible-from">2025-01-01</dd>
<dd data-field="eligible-to">2025-12-31</dd>
<dd data-field="max-funding">400.000,00 EUR</dd>
<dd data-field="status">OPEN</dd>
</dl></body></html>"""

RESTRUCTURED_PAGE = "<html><body><table><tr><td>2025-01-01</td></tr></table></body></html>"


def scraper(handler: Any, **overrides: Any) -> PublicPageScraper:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PublicPageScraper(settings(**overrides), client=client)


class TestPublicPageScraper:
    def setup_method(self) -> None:
        public_page._last_request_at.clear()

    def test_a_well_formed_page_yields_located_fields(self) -> None:
        handler = lambda request: httpx.Response(200, text=GOOD_PAGE)  # noqa: E731
        capture = scraper(handler).capture("http://pages.test/convocatoria.html")
        assert capture.value("call.code") == "CALL-SYN-2025-A"
        assert capture.as_date("call.eligible_from") == date(2025, 1, 1)
        assert capture.as_amount("call.max_funding_eur") == Decimal("400000.00")
        locator = capture.fields[0].locator.model_dump()
        assert locator["kind"] == "HTML_SELECTOR"
        assert locator["selector"]

    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.test/page.html",
            "https://example.com/",
            "file:///etc/passwd",
            "ftp://pages.test/x",
            "http://user:pass@pages.test/x",
        ],
    )
    def test_anything_outside_the_allowlist_is_refused(self, url: str) -> None:
        handler = lambda request: httpx.Response(200, text=GOOD_PAGE)  # noqa: E731
        with pytest.raises(ScraperError):
            scraper(handler).capture(url)

    def test_a_page_over_the_size_ceiling_is_refused(self) -> None:
        handler = lambda request: httpx.Response(200, text="x" * 9000)  # noqa: E731
        with pytest.raises(ScraperError, match="byte ceiling"):
            scraper(handler, scraper_max_bytes=1024).capture("http://pages.test/big.html")

    def test_a_structure_change_is_reported_not_swallowed(self) -> None:
        handler = lambda request: httpx.Response(200, text=RESTRUCTURED_PAGE)  # noqa: E731
        with pytest.raises(StructureChangedError) as excinfo:
            scraper(handler).capture("http://pages.test/changed.html")
        assert "call.eligible_from" in excinfo.value.missing

    def test_malformed_html_does_not_crash_the_parser(self) -> None:
        broken = (
            "<html><body><dl><dd data-field='call-code'>CALL-SYN-2025-A"
            "<dd data-field='eligible-from'>2025-01-01<p>unclosed</body>"
        )
        handler = lambda request: httpx.Response(200, text=broken)  # noqa: E731
        with pytest.raises(StructureChangedError):
            scraper(handler).capture("http://pages.test/broken.html")

    def test_an_http_error_is_classified(self) -> None:
        with pytest.raises(ScraperError) as server_error:
            scraper(lambda request: httpx.Response(500)).capture("http://pages.test/x")
        assert server_error.value.retryable is True

        public_page._last_request_at.clear()
        with pytest.raises(ScraperError) as client_error:
            scraper(lambda request: httpx.Response(404)).capture("http://pages.test/x")
        assert client_error.value.retryable is False

    def test_an_ambiguous_amount_format_is_not_guessed(self) -> None:
        """A dot-decimal amount on a Spanish page is refused, not reinterpreted.

        Reading "400000.00" as four hundred thousand requires assuming the page
        does not use the Spanish convention. The parser does not assume; the
        source declares its format.
        """
        page = GOOD_PAGE.replace("400.000,00 EUR", "400000.00 EUR")
        handler = lambda request: httpx.Response(200, text=page)  # noqa: E731
        capture = scraper(handler).capture("http://pages.test/ambiguous.html")
        assert capture.as_amount("call.max_funding_eur") is None

    def test_the_user_agent_identifies_the_client(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, text=GOOD_PAGE)

        scraper(handler).capture("http://pages.test/x")
        assert "innovation-evidence-pipeline" in captured[0].headers["User-Agent"]

    def test_requests_to_one_host_are_spaced_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr(
            "iep.connectors.public_page.time.sleep", lambda seconds: slept.append(seconds)
        )
        handler = lambda request: httpx.Response(200, text=GOOD_PAGE)  # noqa: E731
        instance = scraper(handler, scraper_min_interval_seconds=5.0)
        instance.capture("http://pages.test/x")
        instance.capture("http://pages.test/x")
        assert slept and slept[0] > 0
