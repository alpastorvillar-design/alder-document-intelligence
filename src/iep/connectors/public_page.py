"""Controlled capture of a published page.

Scraping is the technique most likely to be used carelessly, so the constraints
are in the code rather than in a policy document:

* an allowlist of hosts, checked after URL parsing, with private and loopback
  address ranges rejected unless the host is explicitly allowed. This is the
  SSRF guard: without it, a stored URL is a request-forgery primitive;
* only http and https, no redirects followed, no credentials in the URL;
* a byte ceiling enforced while streaming, so a large response cannot be read
  into memory before the limit is noticed;
* a minimum interval between requests to the same host;
* an identifiable user agent, because an anonymous scraper is a scraper you
  cannot be asked to stop;
* selectors declared as a contract. When a field the contract requires is
  missing, the capture reports a structure change rather than returning None
  and letting a downstream rule conclude the value is absent.

The legal considerations - robots.txt, terms of use, copyright, and taking only
the fields actually needed - are documented in docs/ingestion-and-provenance.md.
In this repository the only host in the allowlist is the local development
simulator.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from iep.config import Settings
from iep.domain.contracts import HtmlSelectorLocator
from iep.extraction import parse

log = logging.getLogger(__name__)

SCRAPER_VERSION = "public-page-scraper/1.0.0"

# The contract: selector, whether it is required, and how to read the value.
FIELD_SELECTORS: dict[str, tuple[str, bool]] = {
    "call.code": ('[data-field="call-code"]', True),
    "call.eligible_from": ('[data-field="eligible-from"]', True),
    "call.eligible_to": ('[data-field="eligible-to"]', True),
    "call.max_funding_eur": ('[data-field="max-funding"]', True),
    "call.status": ('[data-field="status"]', False),
}


class ScraperError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class StructureChangedError(ScraperError):
    """A required selector matched nothing. The page shape changed."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(f"required selectors matched nothing: {', '.join(missing)}")
        self.missing = missing


@dataclass(frozen=True)
class CapturedField:
    field_path: str
    value_text: str
    locator: HtmlSelectorLocator


@dataclass(frozen=True)
class PageCapture:
    url: str
    captured_at: datetime
    html: bytes
    fields: tuple[CapturedField, ...]

    def value(self, field_path: str) -> str | None:
        for field in self.fields:
            if field.field_path == field_path:
                return field.value_text
        return None

    def as_date(self, field_path: str) -> date | None:
        raw = self.value(field_path)
        return parse.parse_date(raw) if raw else None

    def as_amount(self, field_path: str) -> Decimal | None:
        raw = self.value(field_path)
        return parse.parse_amount(raw) if raw else None


_last_request_at: dict[str, float] = {}


class PublicPageScraper:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.allowed_hosts = settings.scraper_allowed_hosts
        self.max_bytes = settings.scraper_max_bytes
        self.timeout = settings.scraper_timeout_seconds
        self.min_interval = settings.scraper_min_interval_seconds
        self.user_agent = settings.scraper_user_agent
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def capture(self, url: str) -> PageCapture:
        host = self._check_url(url)
        self._throttle(host)

        try:
            with self._http().stream(
                "GET", url, headers={"User-Agent": self.user_agent}
            ) as response:
                if response.status_code >= 400:
                    raise ScraperError(
                        f"page returned {response.status_code}",
                        retryable=response.status_code >= 500,
                    )
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ScraperError(
                            f"page exceeds the {self.max_bytes} byte ceiling", retryable=False
                        )
                    chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise ScraperError("page request timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ScraperError(f"transport error: {type(exc).__name__}", retryable=True) from exc

        html = b"".join(chunks)
        captured_at = datetime.now(UTC)
        fields, missing = self._parse(html, url, captured_at)
        if missing:
            log.warning("scraper_structure_changed", extra={"missing": list(missing), "url": url})
            raise StructureChangedError(missing)
        return PageCapture(url=url, captured_at=captured_at, html=html, fields=fields)

    def _parse(
        self, html: bytes, url: str, captured_at: datetime
    ) -> tuple[tuple[CapturedField, ...], tuple[str, ...]]:
        # html.parser is the stdlib parser: no external entity resolution and no
        # C parser to feed untrusted markup to.
        soup = BeautifulSoup(html, "html.parser")
        found: list[CapturedField] = []
        missing: list[str] = []
        for field_path, (selector, required) in FIELD_SELECTORS.items():
            element = soup.select_one(selector)
            if element is None:
                if required:
                    missing.append(field_path)
                continue
            text = parse.normalise_whitespace(element.get_text())
            if not text:
                if required:
                    missing.append(field_path)
                continue
            found.append(
                CapturedField(
                    field_path=field_path,
                    value_text=text[:500],
                    locator=HtmlSelectorLocator(
                        url=url, selector=selector, captured_at=captured_at, snippet=text[:200]
                    ),
                )
            )
        return tuple(found), tuple(missing)

    def _check_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ScraperError(f"scheme {parsed.scheme!r} is not allowed")
        if parsed.username or parsed.password:
            raise ScraperError("credentials in the URL are not allowed")
        host = (parsed.hostname or "").lower()
        if not host:
            raise ScraperError("URL has no host")
        if host not in self.allowed_hosts:
            raise ScraperError(f"host {host!r} is not in the scraper allowlist")
        self._reject_unexpected_private_address(host)
        return host

    def _reject_unexpected_private_address(self, host: str) -> None:
        """Resolve and check the address the host actually points at.

        An allowlisted name that resolves to a link-local or metadata address is
        the classic SSRF pivot. Loopback is tolerated because the development
        simulator is on it and is itself allowlisted by name.
        """
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            # Container DNS may not resolve a compose service name from every
            # context; the allowlist has already constrained the host.
            return
        for info in infos:
            address = ipaddress.ip_address(info[4][0])
            if address.is_link_local or address.is_multicast or address.is_reserved:
                raise ScraperError(f"host {host!r} resolves to a disallowed address")

    def _throttle(self, host: str) -> None:
        previous = _last_request_at.get(host)
        now = time.monotonic()
        if previous is not None:
            wait = self.min_interval - (now - previous)
            if wait > 0:
                time.sleep(wait)
        _last_request_at[host] = time.monotonic()
