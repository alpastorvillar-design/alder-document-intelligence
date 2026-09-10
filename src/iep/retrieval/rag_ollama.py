"""Grounded generation against a local Ollama server.

This is the backend to reach for when demonstrating the boundary. Nothing
leaves the machine: no key, no egress, no bill, and the corpus is synthetic
anyway. And unlike either assistant CLI, Ollama accepts a **JSON schema** and
enforces it server-side, so the reply arrives in the answer contract's shape
instead of being coaxed into it by instructions and then salvaged by a parser.

It is still a local model of modest size. It is slower than a hosted call on
this hardware - twenty-five to sixty seconds for a five-segment context - and
its answers are weaker. Both are honest properties of running a model on a
laptop, and the screen reports the model that produced each answer so a reader
can weigh it.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
from pydantic import ValidationError

from iep.retrieval.embeddings import ollama_reason
from iep.retrieval.prompting import screen, user_message
from iep.retrieval.rag import (
    RAG_PROMPT_VERSION,
    ProviderAnswer,
    RagConfigurationError,
    RagGeneration,
    RagProviderError,
    bounded_evidence,
    system_prompt,
)
from iep.retrieval.search import EvidenceHit

# The same contract the hosted provider constrains its reply with. Written out
# here rather than derived from the Pydantic model: what the server enforces
# should be readable next to what validates the result, and `additionalProperties`
# false is the part that stops a model adding a field nobody checks.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "sufficient_evidence": {"type": "boolean"},
    },
    "required": ["answer", "citations", "sufficient_evidence"],
    "additionalProperties": False,
}


class OllamaRagGenerator:
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        max_context_chars: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not model:
            raise RagConfigurationError("El backend de Ollama necesita un modelo.")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_context_chars = max_context_chars
        self.transport = transport

    def generate(self, question: str, hits: list[EvidenceHit]) -> RagGeneration:
        screened = screen(bounded_evidence(hits, self.max_context_chars))
        allowed_ids = {item["evidence_id"] for item in screened.items}
        instructions = system_prompt()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": user_message(question, screened)},
            ],
            "format": ANSWER_SCHEMA,
            "stream": False,
            # A local model's thinking tokens are minutes on this hardware and
            # nothing reads them.
            "think": False,
            "options": {"temperature": 0, "num_predict": self.max_output_tokens},
        }

        body = self._post(payload)
        content = body.get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RagProviderError("Ollama devolvió una respuesta vacía.")
        try:
            parsed = ProviderAnswer.model_validate_json(content)
        except ValidationError as exc:
            raise RagProviderError(
                "Ollama devolvió una respuesta que no cumple el contrato."
            ) from exc

        citation_ids = tuple(dict.fromkeys(parsed.citations))
        if not set(citation_ids).issubset(allowed_ids):
            raise RagProviderError("The RAG provider invented an evidence citation.")

        return RagGeneration(
            answer=parsed.answer,
            citation_ids=citation_ids,
            sufficient_evidence=parsed.sufficient_evidence,
            provider=f"{self.name}",
            model=self.model,
            input_tokens=_count(body.get("prompt_eval_count")),
            output_tokens=_count(body.get("eval_count")),
            prompt_version=RAG_PROMPT_VERSION,
            prompt_sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
            withheld_directives=screened.withheld,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise RagProviderError(
                f"El modelo local no respondió en {self.timeout_seconds:.0f} segundos. "
                f"Un modelo grande en CPU puede tardar más que eso."
            ) from exc
        except httpx.HTTPError as exc:
            raise RagConfigurationError(
                f"No se pudo hablar con Ollama en {self.base_url}. ¿Está arrancado?"
            ) from exc

        if response.status_code == 404:
            raise RagConfigurationError(
                f"Ollama no tiene el modelo «{self.model}». Descárgalo con "
                f"`ollama pull {self.model}`."
            )
        if response.status_code >= 400:
            # The reason is in the body, not the status. "HTTP 500" on its own
            # was the entire diagnostic for something the server described.
            raise RagProviderError(f"Ollama rechazó la petición: {ollama_reason(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise RagProviderError("Ollama devolvió algo que no es JSON.") from exc
        if not isinstance(body, dict):
            raise RagProviderError("Ollama devolvió algo que no es JSON.")
        return body


def _count(value: object) -> int | None:
    return value if isinstance(value, int) else None
