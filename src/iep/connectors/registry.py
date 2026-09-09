"""HTTP connector for the corporate personnel registry.

The behaviour that matters is not "call an endpoint": it is what happens when
the endpoint misbehaves. Pagination is followed to exhaustion under a page
ceiling, 5xx and 429 are retried with exponential backoff while 4xx is not,
`Retry-After` is honoured, every response is validated against a versioned
contract before a single field is used, and the whole capture is content-hashed
so the same snapshot ingested twice is one document, not two.

The token is read from configuration and never logged.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from iep.config import Settings
from iep.observability import metrics

log = logging.getLogger(__name__)

CONNECTOR_VERSION = "registry-connector/1.0.0"
SUPPORTED_CONTRACT = "registry/v1"
MAX_PAGES = 50


class RegistryPerson(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    employee_id: str = Field(min_length=1, max_length=32)
    full_name: str = Field(min_length=1, max_length=200)
    role: str = Field(max_length=120)
    hourly_rate_eur: Decimal = Field(ge=0)
    contract_start: date
    contract_end: date | None = None

    def covers(self, day: date) -> bool:
        if day < self.contract_start:
            return False
        return self.contract_end is None or day <= self.contract_end


class RegistryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    page: int
    page_size: int
    total: int
    has_more: bool
    items: tuple[RegistryPerson, ...]


@dataclass(frozen=True)
class RegistrySnapshot:
    people: tuple[RegistryPerson, ...]
    contract_version: str
    endpoint: str
    raw_payload: bytes
    pages_fetched: int
    attempts: int

    def by_id(self) -> dict[str, RegistryPerson]:
        return {person.employee_id: person for person in self.people}


class ConnectorError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class RegistryConnector:
    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.base_url = settings.registry_api_base_url.rstrip("/")
        self.token = settings.registry_api_token
        self.page_size = settings.registry_api_page_size
        self.timeout = settings.registry_api_timeout_seconds
        self.max_attempts = max(1, settings.registry_api_max_attempts)
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"User-Agent": f"innovation-evidence-pipeline ({CONNECTOR_VERSION})"},
                follow_redirects=False,
            )
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def fetch_personnel(self) -> RegistrySnapshot:
        people: list[RegistryPerson] = []
        raw_pages: list[dict[str, object]] = []
        attempts_total = 0
        page = 1

        while page <= MAX_PAGES:
            payload, attempts = self._get(
                "/api/v1/personnel", params={"page": page, "page_size": self.page_size}
            )
            attempts_total += attempts
            try:
                parsed = RegistryPage.model_validate(payload)
            except ValidationError as exc:
                raise ConnectorError(
                    f"registry response failed contract validation: {exc.error_count()} errors",
                    retryable=False,
                ) from exc

            if parsed.contract_version != SUPPORTED_CONTRACT:
                # Refusing an unknown contract version is the point of sending
                # one: silently reading a changed shape is how bad data enters.
                raise ConnectorError(
                    f"registry speaks {parsed.contract_version}, this connector speaks "
                    f"{SUPPORTED_CONTRACT}",
                    retryable=False,
                )

            people.extend(parsed.items)
            raw_pages.append(payload)
            if not parsed.has_more:
                break
            page += 1
        else:
            raise ConnectorError(f"registry paginated past {MAX_PAGES} pages", retryable=False)

        snapshot = {
            "source": f"{self.base_url}/api/v1/personnel",
            "contract_version": SUPPORTED_CONTRACT,
            "connector_version": CONNECTOR_VERSION,
            "pages": raw_pages,
        }
        return RegistrySnapshot(
            people=tuple(people),
            contract_version=SUPPORTED_CONTRACT,
            endpoint="/api/v1/personnel",
            # Sorted keys so an unchanged registry always hashes to the same
            # bytes and re-capturing it is a deduplicated no-op.
            raw_payload=json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8"),
            pages_fetched=page,
            attempts=attempts_total,
        )

    def _get(self, path: str, *, params: dict[str, str | int]) -> tuple[dict[str, object], int]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._http().get(
                    path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "User-Agent": f"innovation-evidence-pipeline ({CONNECTOR_VERSION})",
                    },
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                metrics.increment("iep_connector_errors_total", kind="timeout")
                self._backoff(attempt, None)
                continue
            except httpx.TransportError as exc:
                last_error = exc
                metrics.increment("iep_connector_errors_total", kind="transport")
                self._backoff(attempt, None)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = ConnectorError(
                    f"registry returned {response.status_code}", retryable=True
                )
                metrics.increment("iep_connector_errors_total", kind=f"http_{response.status_code}")
                log.warning(
                    "registry_retryable_status",
                    extra={"status": response.status_code, "attempt": attempt},
                )
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue

            if response.status_code >= 400:
                # A 4xx means the request is wrong. Retrying sends the same one.
                raise ConnectorError(
                    f"registry rejected the request with {response.status_code}", retryable=False
                )

            try:
                body = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise ConnectorError("registry returned a non-JSON body", retryable=False) from exc
            if not isinstance(body, dict):
                raise ConnectorError("registry returned a non-object body", retryable=False)
            metrics.increment("iep_connector_requests_total", endpoint=path)
            return body, attempt

        raise ConnectorError(
            f"registry did not answer after {self.max_attempts} attempts: {last_error}",
            retryable=True,
        )

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 5.0))
                return
            except (TypeError, ValueError, InvalidOperation):
                pass
        time.sleep(min(0.2 * (2 ** (attempt - 1)), 2.0))
