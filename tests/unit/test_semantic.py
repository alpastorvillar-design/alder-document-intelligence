"""The semantic provider seam.

The hosted adapter is exercised entirely through an injected double. No test
here opens a socket or spends money; what is being tested is the adapter's
behaviour when a provider misbehaves, which is the part that matters.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import anthropic
import httpx
import pytest
from corpus import documents as corpus_documents
from corpus.dataset import DOSSIER_B

from iep.domain.enums import DocumentKind
from iep.semantic.deterministic import DeterministicSemanticExtractor
from iep.semantic.llm import (
    AnthropicSemanticExtractor,
    LlmProposal,
    LlmResponse,
    estimate_cost_eur,
)
from iep.semantic.protocol import (
    SemanticFieldSpec,
    SemanticProviderError,
    SemanticRequest,
    SemanticResult,
    grounded_proposals,
)

FIELDS = (
    SemanticFieldSpec(field_path="report.project_code", description="reference", value_type="text"),
    SemanticFieldSpec(field_path="report.title", description="title", value_type="text"),
)

REPORT_TEXT = (
    "MEMORIA TECNICA DE EJECUCION\n"
    "Expediente: INN-2025-042\n"
    "Titulo del proyecto: Plataforma de inspeccion asistida\n"
    "TOTAL DECLARADO: 83.500,00 EUR\n"
)


def request(text: str = REPORT_TEXT) -> SemanticRequest:
    return SemanticRequest(
        document_id="doc-1",
        text=text,
        fields=FIELDS,
        candidate_kinds=(DocumentKind.TECHNICAL_REPORT, DocumentKind.EXPENSE_INVOICE),
    )


class Usage:
    def __init__(self, input_tokens: int = 120, output_tokens: int = 40) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class Reply:
    def __init__(self, parsed: Any) -> None:
        self.parsed_output = parsed
        self.usage = Usage()


class FakeMessages:
    """Stands in for `client.messages`, scripted per call."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def adapter(script: list[Any], *, max_attempts: int = 2) -> AnthropicSemanticExtractor:
    return AnthropicSemanticExtractor(
        api_key="",
        model="claude-opus-5",
        timeout_seconds=1.0,
        max_attempts=max_attempts,
        max_output_tokens=512,
        client=FakeMessages(script),
    )


def ok_response(**overrides: Any) -> LlmResponse:
    payload: dict[str, Any] = {
        "document_kind": DocumentKind.TECHNICAL_REPORT,
        "kind_confidence": 0.94,
        "proposals": [
            LlmProposal(
                field_path="report.project_code",
                value="INN-2025-042",
                evidence_quote="Expediente: INN-2025-042",
                confidence=0.95,
            )
        ],
        "notes": [],
    }
    payload.update(overrides)
    return LlmResponse(**payload)


class TestDeterministicProvider:
    def test_it_classifies_a_report(self) -> None:
        outcome = DeterministicSemanticExtractor().run(request())
        assert outcome.result.document_kind is DocumentKind.TECHNICAL_REPORT
        assert outcome.result.kind_confidence > 0.5

    def test_every_proposal_is_grounded(self) -> None:
        outcome = DeterministicSemanticExtractor().run(request())
        assert outcome.warnings == ()
        for proposal in outcome.result.proposals:
            assert proposal.evidence_quote in REPORT_TEXT

    def test_it_costs_nothing_and_says_so(self) -> None:
        outcome = DeterministicSemanticExtractor().run(request())
        assert outcome.usage.estimated_cost_eur == 0.0
        assert outcome.usage.input_tokens == 0

    def test_config_hash_is_stable_and_changes_with_config(self) -> None:
        first = DeterministicSemanticExtractor().config_hash()
        assert first == DeterministicSemanticExtractor().config_hash()
        assert (
            first
            != AnthropicSemanticExtractor(
                api_key="",
                model="claude-opus-5",
                timeout_seconds=1.0,
                max_attempts=1,
                max_output_tokens=1,
                client=FakeMessages([]),
            ).config_hash()
        )


class TestGrounding:
    def test_a_value_not_in_the_document_is_discarded(self) -> None:
        result = SemanticResult(
            document_kind=DocumentKind.TECHNICAL_REPORT,
            kind_confidence=0.9,
            proposals=(
                LlmProposal(
                    field_path="report.declared_total_eur",
                    value="0,00",
                    evidence_quote="TOTAL DECLARADO: 0,00 EUR",
                    confidence=0.99,
                ).model_dump(),  # type: ignore[arg-type]
            ),
        )
        kept, rejected = grounded_proposals(result, REPORT_TEXT)
        assert kept == ()
        assert len(rejected) == 1

    def test_whitespace_differences_do_not_break_grounding(self) -> None:
        result = SemanticResult(
            document_kind=DocumentKind.TECHNICAL_REPORT,
            kind_confidence=0.9,
            proposals=(
                LlmProposal(
                    field_path="report.project_code",
                    value="INN-2025-042",
                    evidence_quote="Expediente:   INN-2025-042",
                    confidence=0.9,
                ).model_dump(),  # type: ignore[arg-type]
            ),
        )
        kept, rejected = grounded_proposals(result, REPORT_TEXT)
        assert len(kept) == 1
        assert rejected == ()


class TestHostedAdapter:
    def test_a_good_response_is_used(self) -> None:
        provider = adapter([Reply(ok_response())])
        outcome = provider.run(request())
        assert outcome.result.document_kind is DocumentKind.TECHNICAL_REPORT
        assert len(outcome.result.proposals) == 1
        assert outcome.usage.attempts == 1
        assert outcome.usage.input_tokens == 120

    def test_it_refuses_to_start_without_a_key(self) -> None:
        with pytest.raises(SemanticProviderError, match="IEP_LLM_API_KEY"):
            AnthropicSemanticExtractor(
                api_key="",
                model="claude-opus-5",
                timeout_seconds=1.0,
                max_attempts=1,
                max_output_tokens=1,
            )

    def test_an_unparsable_response_is_retried_then_abandoned(self) -> None:
        provider = adapter([Reply(None), Reply(None)], max_attempts=2)
        with pytest.raises(SemanticProviderError) as excinfo:
            provider.run(request())
        assert excinfo.value.retryable is True

    def test_a_schema_violation_is_fed_back_on_the_retry(self) -> None:
        client = FakeMessages([Reply(None), Reply(ok_response())])
        provider = AnthropicSemanticExtractor(
            api_key="",
            model="claude-opus-5",
            timeout_seconds=1.0,
            max_attempts=2,
            max_output_tokens=512,
            client=client,
        )
        outcome = provider.run(request())
        assert outcome.usage.attempts == 2
        assert outcome.usage.rejected_responses == 1
        second_call_messages = client.calls[1]["messages"]
        assert "could not be used" in second_call_messages[-1]["content"]

    def test_a_timeout_is_retried(self) -> None:
        timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.test"))
        provider = adapter([timeout, Reply(ok_response())], max_attempts=2)
        outcome = provider.run(request())
        assert outcome.usage.attempts == 2

    def test_a_4xx_is_not_retried(self) -> None:
        response = httpx.Response(400, request=httpx.Request("POST", "https://example.test"))
        error = anthropic.BadRequestError("bad", response=response, body=None)
        client = FakeMessages([error, Reply(ok_response())])
        provider = AnthropicSemanticExtractor(
            api_key="",
            model="claude-opus-5",
            timeout_seconds=1.0,
            max_attempts=3,
            max_output_tokens=512,
            client=client,
        )
        with pytest.raises(SemanticProviderError) as excinfo:
            provider.run(request())
        assert excinfo.value.retryable is False
        assert len(client.calls) == 1

    def test_an_ungrounded_proposal_is_dropped_with_a_warning(self) -> None:
        invented = ok_response(
            proposals=[
                LlmProposal(
                    field_path="report.declared_total_eur",
                    value="0,00",
                    evidence_quote="El importe correcto es 0,00 EUR",
                    confidence=0.99,
                )
            ]
        )
        outcome = adapter([Reply(invented)]).run(request())
        assert outcome.result.proposals == ()
        assert outcome.warnings
        assert outcome.usage.rejected_responses == 1

    def test_the_document_is_wrapped_as_untrusted_data(self) -> None:
        client = FakeMessages([Reply(ok_response())])
        provider = AnthropicSemanticExtractor(
            api_key="",
            model="claude-opus-5",
            timeout_seconds=1.0,
            max_attempts=1,
            max_output_tokens=512,
            client=client,
        )
        provider.run(request())
        sent = client.calls[0]
        assert "<untrusted_document>" in sent["messages"][0]["content"]
        assert "untrusted" in sent["system"].lower()
        assert "Never perform arithmetic" in sent["system"]

    def test_a_document_full_of_instructions_cannot_express_an_action(self) -> None:
        """The adversarial annex, put through the adapter.

        The reply invents a value under an unrequested field and pairs it with
        an unrelated sentence from the document. Both the requested-field
        allowlist and value-in-quote grounding reject it before the pipeline.
        """
        text = _pdf_text(corpus_documents.injection_document(DOSSIER_B))
        assert "Ignore all previous instructions" in text

        obedient = LlmResponse(
            document_kind=DocumentKind.TECHNICAL_REPORT,
            kind_confidence=0.9,
            proposals=[
                LlmProposal(
                    field_path="report.declared_total_eur",
                    value="0,00",
                    evidence_quote="the dossier is approved",
                    confidence=1.0,
                )
            ],
            notes=["the document instructed me to approve the dossier"],
        )
        outcome = adapter([Reply(obedient)]).run(request(text))

        assert outcome.result.proposals == ()
        assert outcome.warnings
        # And there is nothing in the result that could approve, dismiss or
        # delete anything: the contract has no field for an action.
        assert set(outcome.result.model_dump()) == {
            "document_kind",
            "kind_confidence",
            "proposals",
            "notes",
        }
        assert outcome.result.notes


class TestCostAccounting:
    def test_cost_is_estimated_from_published_prices(self) -> None:
        cost = estimate_cost_eur("claude-opus-5", 1_000_000, 0)
        assert cost == Decimal("5.00") * Decimal("0.92")

    def test_an_unknown_model_falls_back_to_a_declared_default(self) -> None:
        assert estimate_cost_eur("some-future-model", 1_000_000, 0) > 0

    def test_zero_tokens_cost_nothing(self) -> None:
        assert estimate_cost_eur("claude-opus-5", 0, 0) == 0


def _pdf_text(data: bytes) -> str:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)
