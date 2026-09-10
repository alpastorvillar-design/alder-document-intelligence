"""A learned embedding model, locally, and the floor it makes possible.

The hashing baseline proved the pgvector plumbing and nothing else: its scores
do not separate a relevant query from an irrelevant one, so vector search
returned `limit` rows whatever was asked. A learned multilingual model changes
that, which is why these two things arrive together - the provider, and the
relevance floor whose default depends on which provider is in use.

The model is not called here. What is tested is the contract with it: the
width comes from the response rather than from configuration, a width that
changes mid-run is refused rather than written, and the width is part of the
configuration hash so rows made two different ways can never be compared.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from iep.config import LEARNED_MIN_SIMILARITY, Settings
from iep.retrieval.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingProviderError,
    HashingEmbeddingProvider,
    OllamaEmbeddingProvider,
    build_embedding_provider,
)


def responder(*batches: list[list[float]], status: int = 200) -> httpx.MockTransport:
    """Replies with each batch in turn, so a width change can be simulated."""
    remaining = list(batches)

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "no"})
        body = json.loads(request.content)
        vectors = remaining.pop(0) if remaining else []
        assert request.url.path == "/api/embed"
        assert body["model"]
        return httpx.Response(
            200, json={"embeddings": vectors, "prompt_eval_count": 7 * len(body["input"])}
        )

    return httpx.MockTransport(handler)


def provider(transport: httpx.MockTransport, model: str = "bge-m3") -> OllamaEmbeddingProvider:
    return OllamaEmbeddingProvider(
        base_url="http://localhost:11434",
        model=model,
        timeout_seconds=5.0,
        transport=transport,
    )


class TestTheWidthComesFromTheModel:
    def test_it_is_discovered_from_the_first_response(self) -> None:
        """A configured width that disagreed with the model would write every
        vector at the wrong shape, and every later comparison would fail - or,
        worse, succeed against the wrong rows."""
        one = provider(responder([[0.1] * 1024]))
        batch = one.embed(["gastos de personal"])
        assert len(batch.vectors[0]) == 1024
        assert one.dimensions == 1024

    def test_a_different_model_reports_a_different_width(self) -> None:
        wide = provider(responder([[0.2] * 2560]), model="qwen3-embedding:4b")
        wide.embed(["gastos de personal"])
        assert wide.dimensions == 2560

    def test_the_width_is_part_of_the_configuration_hash(self) -> None:
        """Rows made at one width must never be selected for comparison with
        rows made at another. The hash is what enforces that, so the width has
        to be in it."""
        narrow = provider(responder([[0.1] * 1024]))
        wide = provider(responder([[0.1] * 2560]))
        assert narrow.config_hash() != wide.config_hash()

    def test_a_width_change_mid_run_is_refused(self) -> None:
        one = provider(responder([[0.1] * 1024], [[0.1] * 512]))
        one.embed(["primero"])
        with pytest.raises(EmbeddingProviderError, match="ha cambiado de 1024 a 512"):
            one.embed(["segundo"])

    def test_mixed_widths_in_one_batch_are_refused(self) -> None:
        with pytest.raises(EmbeddingProviderError, match="anchuras distintas"):
            provider(responder([[0.1] * 1024, [0.1] * 512])).embed(["a", "b"])

    def test_a_short_reply_is_refused(self) -> None:
        """Two texts in, one vector out, means nobody can tell which is which."""
        with pytest.raises(EmbeddingProviderError, match="distinto al de textos"):
            provider(responder([[0.1] * 1024])).embed(["a", "b"])

    def test_an_empty_vector_is_refused(self) -> None:
        with pytest.raises(EmbeddingProviderError, match="vector vacío"):
            provider(responder([[]])).embed(["a"])

    def test_no_texts_means_no_call(self) -> None:
        batch = provider(responder()).embed([])
        assert batch.vectors == ()

    def test_the_token_count_is_kept(self) -> None:
        batch = provider(responder([[0.1] * 1024, [0.2] * 1024])).embed(["a", "b"])
        assert batch.input_tokens == 14


class TestWhatItSaysWhenItCannotWork:
    def test_a_missing_model_names_the_pull_command(self) -> None:
        with pytest.raises(EmbeddingConfigurationError, match="ollama pull"):
            provider(responder(status=404)).embed(["a"])

    def test_no_model_configured_says_which_variable(self) -> None:
        with pytest.raises(EmbeddingConfigurationError, match="IEP_OLLAMA_EMBEDDING_MODEL"):
            OllamaEmbeddingProvider(
                base_url="http://localhost:11434", model="", timeout_seconds=1.0
            )

    def test_a_server_that_is_not_there_says_so(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        with pytest.raises(EmbeddingConfigurationError, match=r"¿Está arrancado\?"):
            provider(httpx.MockTransport(refuse)).embed(["a"])

    def test_it_needs_no_egress_opt_in(self) -> None:
        """`IEP_ALLOW_EXTERNAL_AI` is consent to send dossier text off the
        machine. A local model sends nothing, so requiring the flag would only
        teach people to set it."""
        built = build_embedding_provider(
            Settings(embedding_provider="ollama", allow_external_ai=False)
        )
        assert isinstance(built, OllamaEmbeddingProvider)

    def test_the_baseline_is_still_the_default(self) -> None:
        assert isinstance(build_embedding_provider(Settings()), HashingEmbeddingProvider)


class TestTheFloorFollowsTheProvider:
    """One global default was the trap.

    A threshold that protects a learned model destroys the baseline - measured
    on this corpus, the baseline scores a relevant query as low as 0.16 - and
    one safe for the baseline does nothing at all.
    """

    def test_the_baseline_gets_no_floor(self) -> None:
        assert Settings(embedding_provider="hashing").effective_min_similarity == 0.0

    def test_disabled_gets_no_floor(self) -> None:
        assert Settings(embedding_provider="disabled").effective_min_similarity == 0.0

    def test_a_learned_provider_gets_one(self) -> None:
        for name in ("ollama", "openai"):
            settings = Settings(embedding_provider=name)
            assert settings.effective_min_similarity == LEARNED_MIN_SIMILARITY

    def test_an_explicit_value_wins_either_way(self) -> None:
        assert (
            Settings(
                embedding_provider="hashing", retrieval_min_similarity=0.3
            ).effective_min_similarity
            == 0.3
        )
        assert (
            Settings(
                embedding_provider="ollama", retrieval_min_similarity=0.0
            ).effective_min_similarity
            == 0.0
        )

    def test_the_default_is_off_and_that_is_the_measurement(self) -> None:
        """Two earlier versions of this test asserted a tuned number.

        The first said the default sat inside a gap between relevant and
        irrelevant scores. The second, after a wider query set closed that
        gap, said it stayed below the worst relevant score. A third widening
        pushed the worst relevant hit to 0.4968 - below two irrelevant ones -
        so 0.50 was cutting a legitimate question and passing noise anyway.
        Each sample gave a different answer, which is itself the finding.

        What survives is the asymmetry: noise reaching the generator is
        recoverable, because it answers that the evidence does not support the
        question. Evidence removed before the generator sees it is not - it
        cannot report an absence it was never shown.
        """
        assert LEARNED_MIN_SIMILARITY == 0.0
        assert LEARNED_MIN_SIMILARITY < 0.4968

    def test_the_filter_still_exists_for_whoever_measures_their_own(self) -> None:
        """Off by default is not the same as absent. Without the filter,
        vector search has no way to say "nothing here matches"."""
        settings = Settings(embedding_provider="ollama", retrieval_min_similarity=0.42)
        assert settings.effective_min_similarity == 0.42


class TestTheBaselineIsUnchanged:
    def test_it_still_reports_its_configured_width(self) -> None:
        """It is the one provider whose width really is a setting."""
        assert HashingEmbeddingProvider(512).dimensions == 512

    def test_it_still_returns_unit_vectors(self) -> None:
        import math

        vector = HashingEmbeddingProvider(512).embed(["gastos de personal"]).vectors[0]
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)

    def test_the_two_providers_never_share_a_hash(self) -> None:
        local = provider(responder([[0.1] * 512]))
        local.embed(["probe"])
        assert local.config_hash() != HashingEmbeddingProvider(512).config_hash()


def test_settings_accept_a_learned_provider() -> None:
    """A validator that refused it was how the column's 512 became a ceiling."""
    settings: Any = Settings(embedding_provider="ollama", ollama_embedding_model="bge-m3")
    assert settings.ollama_embedding_model == "bge-m3"


class TestATransientFailureIsRetriedAndExplained:
    """A live 500 showed both halves of this were missing.

    Loading `bge-m3` alongside an already-resident 9B generation model
    returned HTTP 500 on `/api/embed`, and the same call succeeded moments
    later. The message said "HTTP 500" and nothing else, discarding the
    explanation the server had put in the body.
    """

    def flaky(self, *, failures: int, reason: str = "model is loading") -> httpx.MockTransport:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] <= failures:
                return httpx.Response(500, json={"error": reason})
            return httpx.Response(200, json={"embeddings": [[0.1] * 1024]})

        transport = httpx.MockTransport(handler)
        transport.attempts = attempts  # type: ignore[attr-defined]
        return transport

    def test_one_transient_failure_is_survived(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("iep.retrieval.embeddings.time.sleep", lambda _: None)
        transport = self.flaky(failures=1)
        batch = provider(transport).embed(["gastos de personal"])
        assert len(batch.vectors[0]) == 1024
        assert transport.attempts["n"] == 2  # type: ignore[attr-defined]

    def test_it_does_not_retry_for_ever(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A machine that cannot hold both models will not start being able
        to, so this reports rather than waits."""
        monkeypatch.setattr("iep.retrieval.embeddings.time.sleep", lambda _: None)
        transport = self.flaky(failures=99)
        with pytest.raises(EmbeddingProviderError, match="tras 2 intentos"):
            provider(transport).embed(["gastos de personal"])
        assert transport.attempts["n"] == 2  # type: ignore[attr-defined]

    def test_the_servers_own_words_reach_the_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("iep.retrieval.embeddings.time.sleep", lambda _: None)
        with pytest.raises(EmbeddingProviderError, match="no hay memoria suficiente"):
            provider(self.flaky(failures=99, reason="no hay memoria suficiente")).embed(["a"])

    def test_a_client_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 400 means the request is wrong, and repeating it changes nothing."""
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(400, json={"error": "input demasiado largo"})

        with pytest.raises(EmbeddingProviderError, match="input demasiado largo"):
            provider(httpx.MockTransport(handler)).embed(["a"])
        assert attempts["n"] == 1

    def test_a_body_without_a_reason_falls_back_to_the_status(self) -> None:
        from iep.retrieval.embeddings import ollama_reason

        assert ollama_reason(httpx.Response(503, text="<html>gateway</html>")) == "HTTP 503"
        assert ollama_reason(httpx.Response(500, json={"error": "  "})) == "HTTP 500"
        assert ollama_reason(httpx.Response(500, json={"error": "cargando"})) == "cargando"


class TestAnEmptyEnvironmentVariableDoesNotStopTheProcess:
    """`IEP_RETRIEVAL_MIN_SIMILARITY=` made the API container exit.

    Compose writes `${VAR:-}` as an empty string, and so does a `.env` line
    with nothing after the `=`, and so does an exported empty value. Pydantic
    read `""` as "not a number" and refused to build the settings, so the
    process died before serving a request - a worse failure than anything the
    setting itself could cause.
    """

    def test_an_empty_string_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP_RETRIEVAL_MIN_SIMILARITY", "")
        settings = Settings(embedding_provider="ollama")
        assert settings.retrieval_min_similarity is None
        assert settings.effective_min_similarity == LEARNED_MIN_SIMILARITY

    def test_whitespace_is_treated_as_unset_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP_RETRIEVAL_MIN_SIMILARITY", "   ")
        assert Settings(embedding_provider="hashing").effective_min_similarity == 0.0

    def test_a_real_value_still_arrives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP_RETRIEVAL_MIN_SIMILARITY", "0.31")
        assert Settings(embedding_provider="ollama").effective_min_similarity == 0.31

    def test_a_value_that_is_not_a_number_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tolerating blank must not mean tolerating nonsense: a typo in a
        threshold should stop the process, because silently ignoring it would
        run with a filter nobody chose."""
        monkeypatch.setenv("IEP_RETRIEVAL_MIN_SIMILARITY", "cero-coma-cinco")
        with pytest.raises(ValidationError):
            Settings()

    def test_out_of_range_is_still_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("IEP_RETRIEVAL_MIN_SIMILARITY", "1.5")
        with pytest.raises(ValidationError):
            Settings()
