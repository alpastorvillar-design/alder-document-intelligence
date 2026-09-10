"""Optional, read-only grounded generation over retrieved evidence.

The endpoint sends only the selected chunks, not an entire dossier. Retrieved
document text is serialised as untrusted JSON data. The provider receives no
tools and cannot approve, edit or transition a dossier.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from iep.config import Settings
from iep.retrieval.search import EvidenceHit

RAG_PROMPT_VERSION = "rag-grounded-answer/1.0.0"


class RagProviderError(RuntimeError):
    retryable = True


class RagConfigurationError(RagProviderError):
    retryable = False


class _ProviderAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=2000)
    citations: list[str] = Field(max_length=10)
    sufficient_evidence: bool

    @model_validator(mode="after")
    def _citations_when_sufficient(self) -> _ProviderAnswer:
        if self.sufficient_evidence and not self.citations:
            raise ValueError("a supported answer needs at least one citation")
        return self


@dataclass(frozen=True)
class RagGeneration:
    answer: str
    citation_ids: tuple[str, ...]
    sufficient_evidence: bool
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    prompt_version: str
    prompt_sha256: str


class OpenAIResponsesRagGenerator:
    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        max_output_tokens: int,
        max_context_chars: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise RagConfigurationError("The RAG provider needs an API key.")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_output_tokens = max_output_tokens
        self.max_context_chars = max_context_chars
        self.transport = transport

    def generate(self, question: str, hits: list[EvidenceHit]) -> RagGeneration:
        evidence = _bounded_evidence(hits, self.max_context_chars)
        allowed_ids = {item["evidence_id"] for item in evidence}
        system_prompt = _system_prompt()
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": system_prompt,
            "input": json.dumps(
                {"question": question, "EVIDENCE_JSON": evidence},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "grounded_dossier_answer",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "answer": {"type": "string"},
                            "citations": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 10,
                            },
                            "sufficient_evidence": {"type": "boolean"},
                        },
                        "required": ["answer", "citations", "sufficient_evidence"],
                    },
                }
            },
        }
        body = self._post(payload)
        raw_text = _output_text(body)
        try:
            parsed = _ProviderAnswer.model_validate_json(raw_text)
        except ValidationError as exc:
            raise RagProviderError("The RAG provider returned an invalid answer contract.") from exc
        citation_ids = tuple(dict.fromkeys(parsed.citations))
        if not set(citation_ids).issubset(allowed_ids):
            raise RagProviderError("The RAG provider invented an evidence citation.")

        usage = body.get("usage")
        input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
        return RagGeneration(
            answer=parsed.answer,
            citation_ids=citation_ids,
            sufficient_evidence=parsed.sufficient_evidence,
            provider=self.name,
            model=self.model,
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
            output_tokens=output_tokens if isinstance(output_tokens, int) else None,
            prompt_version=RAG_PROMPT_VERSION,
            prompt_sha256=hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        )

    def _post(self, payload: dict[str, object]) -> dict[str, Any]:
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = client.post(
                        "/responses",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_attempts:
                    raise RagProviderError("The RAG provider could not be reached.") from exc
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))
                continue

            last_status = response.status_code
            if response.status_code < 400:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise RagProviderError("The RAG provider returned invalid JSON.") from exc
                if not isinstance(body, dict):
                    raise RagProviderError("The RAG provider returned invalid JSON.")
                return body

            retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
            if not retryable:
                raise RagConfigurationError(
                    f"The RAG provider rejected the request (HTTP {response.status_code})."
                )
            if attempt < self.max_attempts:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))

        raise RagProviderError(f"The RAG provider failed after retries (HTTP {last_status}).")


def build_rag_generator(settings: Settings) -> OpenAIResponsesRagGenerator:
    if settings.rag_provider == "disabled":
        raise RagConfigurationError("RAG generation is disabled.")
    if not settings.allow_external_ai:
        raise RagConfigurationError(
            "Hosted RAG requires IEP_ALLOW_EXTERNAL_AI=true as an explicit data-egress opt-in."
        )
    return OpenAIResponsesRagGenerator(
        api_key=settings.openai_api_key,
        model=settings.openai_rag_model,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.openai_timeout_seconds,
        max_attempts=settings.openai_max_attempts,
        max_output_tokens=settings.rag_max_output_tokens,
        max_context_chars=settings.rag_max_context_chars,
    )


def _bounded_evidence(hits: list[EvidenceHit], max_chars: int) -> list[dict[str, object]]:
    remaining = max_chars
    evidence: list[dict[str, object]] = []
    for position, hit in enumerate(hits, start=1):
        if remaining <= 0:
            break
        text = hit.text[:remaining]
        evidence.append(
            {
                "evidence_id": f"E{position}",
                "document": hit.document_name,
                "document_id": str(hit.document_id),
                "ordinal": hit.ordinal,
                "locator": hit.locator,
                "text": text,
            }
        )
        remaining -= len(text)
    return evidence


def _output_text(body: dict[str, Any]) -> str:
    output = body.get("output")
    if not isinstance(output, list):
        raise RagProviderError("The RAG provider returned no output.")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    return text
    raise RagProviderError("The RAG provider returned no output text.")


def _system_prompt() -> str:
    return (
        resources.files("iep.retrieval.prompts")
        .joinpath("rag_system.txt")
        .read_text(encoding="utf-8")
        .strip()
    )
