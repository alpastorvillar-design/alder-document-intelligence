"""What the local generator sends, and why the context size is part of it.

The model is not called here. What is tested is the request: a local answer
took 97 to 151 seconds and read as a hang, because with `num_ctx` unset Ollama
allocates the model's maximum context - 262144 tokens on `qwen3.5:9b` - and a
KV cache that does not fit in VRAM spills to host memory. Sending the size the
prompt actually needs took the same 68-token answer from ~35 s to ~1.2 s. That
number therefore has to reach the payload, and it has to be big enough for the
prompt this system builds.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from iep.config import Settings
from iep.retrieval.rag import RagProviderError
from iep.retrieval.rag_ollama import OllamaRagGenerator
from iep.retrieval.search import EvidenceHit

# About 3.5 characters per token in Spanish, and the system prompt is well
# under 600. Deliberately generous: the assertion is that the window fits the
# prompt, not that it fits it exactly.
CHARS_PER_TOKEN = 3.5
SYSTEM_PROMPT_TOKENS = 600


def hit(text: str = "Periodo de ejecución: 01/03/2024 - 30/11/2024") -> EvidenceHit:
    return EvidenceHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_name="memoria-tecnica.pdf",
        ordinal=0,
        text=text,
        locator={"kind": "PDF_PAGE", "page": 1},
        rank=1.0,
    )


def capturing(
    sink: list[dict[str, Any]], answer: dict[str, Any] | None = None
) -> httpx.MockTransport:
    body = answer or {
        "answer": "Del 01/03/2024 al 30/11/2024.",
        "citations": ["E1"],
        "sufficient_evidence": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        sink.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(body)},
                "prompt_eval_count": 400,
                "eval_count": 68,
            },
        )

    return httpx.MockTransport(handler)


def generator(transport: httpx.MockTransport, **overrides: Any) -> OllamaRagGenerator:
    settings = Settings()
    kwargs: dict[str, Any] = {
        "base_url": "http://localhost:11434",
        "model": "qwen3.5:9b",
        "timeout_seconds": 5.0,
        "max_output_tokens": settings.rag_max_output_tokens,
        "max_context_chars": settings.rag_max_context_chars,
        "num_ctx": settings.ollama_num_ctx,
        "transport": transport,
    }
    kwargs.update(overrides)
    return OllamaRagGenerator(**kwargs)


class TestTheContextWindowIsSentExplicitly:
    def test_the_payload_carries_it(self) -> None:
        sent: list[dict[str, Any]] = []
        generator(capturing(sent), num_ctx=8192).generate("¿Qué periodo declara?", [hit()])
        assert sent[0]["options"]["num_ctx"] == 8192

    def test_it_comes_from_configuration_rather_than_the_model(self) -> None:
        """The point of sending it is to override what the model would choose."""
        sent: list[dict[str, Any]] = []
        generator(capturing(sent), num_ctx=4096).generate("¿Qué periodo declara?", [hit()])
        assert sent[0]["options"]["num_ctx"] == 4096

    def test_the_default_window_fits_the_prompt_this_system_builds(self) -> None:
        """A window too small for the prompt silently truncates the evidence,
        which is worse than slow: the model would answer from a fragment of
        what it was given and cite it as if it were whole."""
        settings = Settings()
        needed = (
            settings.rag_max_context_chars / CHARS_PER_TOKEN
            + settings.rag_max_output_tokens
            + SYSTEM_PROMPT_TOKENS
        )
        assert settings.ollama_num_ctx > needed

    def test_generation_is_still_deterministic_and_bounded(self) -> None:
        sent: list[dict[str, Any]] = []
        settings = Settings()
        generator(capturing(sent)).generate("¿Qué periodo declara?", [hit()])
        options = sent[0]["options"]
        assert options["temperature"] == 0
        assert options["num_predict"] == settings.rag_max_output_tokens
        assert sent[0]["think"] is False
        assert sent[0]["stream"] is False


class TestTheAnswerContract:
    def test_an_invented_citation_is_refused(self) -> None:
        sent: list[dict[str, Any]] = []
        transport = capturing(
            sent,
            {"answer": "Dice esto.", "citations": ["E7"], "sufficient_evidence": True},
        )
        with pytest.raises(RagProviderError, match="invented"):
            generator(transport).generate("¿Qué periodo declara?", [hit()])

    def test_a_reply_that_is_not_the_agreed_shape_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"message": {"content": "Pues del 1 de marzo."}})

        with pytest.raises(RagProviderError, match="contrato"):
            generator(httpx.MockTransport(handler)).generate("¿Y?", [hit()])
