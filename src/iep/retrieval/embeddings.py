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
from typing import Any, Protocol

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

    @property
    def dimensions(self) -> int:
        """How wide this provider's vectors are.

        Read-only on purpose. For the hashing baseline it comes from
        configuration; for a learned model it is a property of the model and
        is discovered from its first response. Nothing should be able to
        assign it, and declaring it as a plain attribute here meant a provider
        that computed it did not satisfy this protocol.
        """
        ...

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


class OllamaEmbeddingProvider:
    """A learned embedding model on this machine, through Ollama.

    This is what the vector path was missing. The hashing baseline is a
    deterministic projection of character trigrams: it proves the pgvector
    plumbing, and `docs/rag.md` carries the measurement showing it cannot
    separate a relevant query from an irrelevant one. A learned multilingual
    model can, the corpus is Spanish, and running it locally costs nothing and
    sends nothing anywhere.

    The dimension is whatever the model returns - 1024 for `bge-m3`, 2560 for
    `qwen3-embedding` - which is why the column no longer fixes one. It is
    discovered from the first response rather than configured, because a
    configured number that disagrees with the model is a silent corruption:
    every vector would be written at the wrong width and every comparison
    would fail or, worse, succeed against the wrong rows.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not model:
            raise EmbeddingConfigurationError(
                "El proveedor de embeddings de Ollama necesita un modelo "
                "(IEP_OLLAMA_EMBEDDING_MODEL), por ejemplo bge-m3."
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        # Filled in from the first response. Part of `config_hash`, so a model
        # that ever changed width would produce a different hash and its old
        # rows would simply stop being selected rather than be compared
        # against vectors of another shape.
        self._dimensions = 0

    @property
    def dimensions(self) -> int:
        if not self._dimensions:
            self.embed(["dimension probe"])
        return self._dimensions

    def config_hash(self) -> str:
        return _config_hash(self.name, self.model, self.dimensions)

    def embed(self, texts: list[str]) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch(vectors=())
        body = self._post({"model": self.model, "input": texts})
        raw = body.get("embeddings")
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EmbeddingProviderError(
                "Ollama devolvió un número de vectores distinto al de textos enviados."
            )
        vectors: list[tuple[float, ...]] = []
        for entry in raw:
            if not isinstance(entry, list) or not entry:
                raise EmbeddingProviderError("Ollama devolvió un vector vacío.")
            vectors.append(tuple(float(value) for value in entry))

        widths = {len(vector) for vector in vectors}
        if len(widths) != 1:
            raise EmbeddingProviderError(
                f"Ollama devolvió vectores de anchuras distintas: {sorted(widths)}."
            )
        width = widths.pop()
        if self._dimensions and width != self._dimensions:
            # The same configuration returning a different width mid-run would
            # write rows that can never be compared with the ones before them.
            raise EmbeddingProviderError(
                f"El modelo «{self.model}» ha cambiado de {self._dimensions} a {width} "
                f"dimensiones a mitad de ejecución."
            )
        self._dimensions = width
        return EmbeddingBatch(
            vectors=tuple(vectors),
            input_tokens=body.get("prompt_eval_count")
            if isinstance(body.get("prompt_eval_count"), int)
            else None,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post("/api/embed", json=payload)
        except httpx.TimeoutException as exc:
            raise EmbeddingProviderError(
                f"Ollama no respondió en {self.timeout_seconds:.0f} segundos."
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingConfigurationError(
                f"No se pudo hablar con Ollama en {self.base_url}. ¿Está arrancado?"
            ) from exc

        if response.status_code == 404:
            raise EmbeddingConfigurationError(
                f"Ollama no tiene el modelo «{self.model}». Descárgalo con "
                f"`ollama pull {self.model}`."
            )
        if response.status_code >= 400:
            raise EmbeddingProviderError(
                f"Ollama rechazó la petición de embeddings (HTTP {response.status_code})."
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError("Ollama devolvió algo que no es JSON.") from exc
        if not isinstance(body, dict):
            raise EmbeddingProviderError("Ollama devolvió algo que no es JSON.")
        return body


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
    if settings.embedding_provider == "ollama":
        # No egress opt-in: the model runs on this machine, so there is
        # nothing to consent to. Requiring the flag anyway would only teach
        # people to set it.
        return OllamaEmbeddingProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_embedding_model,
            timeout_seconds=settings.ollama_timeout_seconds,
        )
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
