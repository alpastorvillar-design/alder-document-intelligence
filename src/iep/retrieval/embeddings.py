"""Embedding providers for pgvector-backed evidence retrieval.

The default provider is deliberately offline and deterministic. It lets the
database, idempotency and ranking code be exercised without a credential, but
it is a feature-hashing baseline rather than a learned semantic model. The
hosted provider is opt-in twice: both an API key and ``allow_external_ai`` are
required before document text can leave the process.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Protocol

import httpx

from iep.config import Settings

HASHING_VERSION = "feature-hashing/1.0.0"


class EmbeddingProviderError(RuntimeError):
    """A safe provider failure; response bodies and submitted text stay out."""

    retryable = True


class EmbeddingConfigurationError(EmbeddingProviderError):
    retryable = False


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None = None


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimensions: int

    def config_hash(self) -> str: ...

    def embed(self, texts: list[str]) -> EmbeddingBatch: ...


class HashingEmbeddingProvider:
    """A deterministic sparse feature projection, not a semantic model."""

    name = "hashing"
    model = HASHING_VERSION

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def config_hash(self) -> str:
        return _config_hash(self.name, self.model, self.dimensions)

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        return EmbeddingBatch(vectors=tuple(self._one(text) for text in texts))

    def _one(self, text: str) -> tuple[float, ...]:
        normalised = "".join(
            character
            for character in unicodedata.normalize("NFKD", text.casefold())
            if not unicodedata.combining(character)
        )
        words = re.findall(r"[a-z0-9]+", normalised)
        features = [f"w:{word}" for word in words]
        for word in words:
            padded = f"^{word}$"
            features.extend(f"c3:{padded[i : i + 3]}" for i in range(max(1, len(padded) - 2)))
        if not features:
            features = ["<empty>"]

        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector))
        return tuple(value / magnitude for value in vector)


class OpenAIEmbeddingProvider:
    """Small direct adapter for the documented ``POST /v1/embeddings`` contract."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise EmbeddingConfigurationError("The OpenAI embedding provider needs an API key.")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.transport = transport

    def config_hash(self) -> str:
        return _config_hash(self.name, self.model, self.dimensions)

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(vectors=())
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
            "dimensions": self.dimensions,
        }
        body = self._post("/embeddings", payload)
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingProviderError("The embedding provider returned an invalid item count.")

        indexed: dict[int, tuple[float, ...]] = {}
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("index"), int):
                raise EmbeddingProviderError("The embedding provider returned an invalid item.")
            raw_vector = item.get("embedding")
            if not isinstance(raw_vector, list) or len(raw_vector) != self.dimensions:
                raise EmbeddingProviderError(
                    "The embedding provider returned a wrong-sized vector."
                )
            vector: list[float] = []
            for value in raw_vector:
                if not isinstance(value, int | float) or not math.isfinite(float(value)):
                    raise EmbeddingProviderError(
                        "The embedding provider returned a non-finite vector."
                    )
                vector.append(float(value))
            indexed[item["index"]] = tuple(vector)

        if set(indexed) != set(range(len(texts))):
            raise EmbeddingProviderError("The embedding provider returned invalid item indexes.")
        usage = body.get("usage")
        input_tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        return EmbeddingBatch(
            vectors=tuple(indexed[index] for index in range(len(texts))),
            input_tokens=input_tokens if isinstance(input_tokens, int) else None,
        )

    def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                with httpx.Client(
                    base_url=self.base_url,
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = client.post(
                        path,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self.max_attempts:
                    raise EmbeddingProviderError(
                        "The embedding provider could not be reached."
                    ) from exc
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))
                continue

            last_status = response.status_code
            if response.status_code < 400:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise EmbeddingProviderError(
                        "The embedding provider returned invalid JSON."
                    ) from exc
                if not isinstance(body, dict):
                    raise EmbeddingProviderError("The embedding provider returned invalid JSON.")
                return body

            retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
            if not retryable:
                error = EmbeddingConfigurationError(
                    f"The embedding provider rejected the request (HTTP {response.status_code})."
                )
                raise error
            if attempt < self.max_attempts:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))

        raise EmbeddingProviderError(
            f"The embedding provider failed after retries (HTTP {last_status})."
        )


def build_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    if settings.embedding_provider == "disabled":
        return None
    if settings.embedding_provider == "hashing":
        return HashingEmbeddingProvider(settings.embedding_dimensions)
    if not settings.allow_external_ai:
        raise EmbeddingConfigurationError(
            "Hosted embeddings require IEP_ALLOW_EXTERNAL_AI=true "
            "as an explicit data-egress opt-in."
        )
    return OpenAIEmbeddingProvider(
        api_key=settings.openai_api_key,
        model=settings.openai_embedding_model,
        dimensions=settings.embedding_dimensions,
        base_url=settings.openai_base_url,
        timeout_seconds=settings.openai_timeout_seconds,
        max_attempts=settings.openai_max_attempts,
    )


def embed_in_batches(
    provider: EmbeddingProvider, texts: list[str], *, batch_size: int
) -> list[tuple[float, ...]]:
    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(texts), batch_size):
        batch = provider.embed(texts[start : start + batch_size])
        vectors.extend(batch.vectors)
    if len(vectors) != len(texts):
        raise EmbeddingProviderError("The embedding provider returned an invalid total item count.")
    return vectors


def _config_hash(provider: str, model: str, dimensions: int) -> str:
    encoded = json.dumps(
        {"provider": provider, "model": model, "dimensions": dimensions},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
