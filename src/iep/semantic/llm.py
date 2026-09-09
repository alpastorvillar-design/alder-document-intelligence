"""Hosted-model semantic provider.

Written against the official Anthropic SDK and exercised only against injected
test doubles: no test, demo or evaluation path in this repository calls a
billable endpoint, and the provider refuses to construct without an explicit
API key so it cannot be selected by accident.

What makes this an adapter rather than a prompt:

* the response shape is a Pydantic model passed to `messages.parse`, so a reply
  that does not fit the schema never reaches the pipeline;
* the document is wrapped in a data envelope and the instructions say, in the
  system prompt, that its content is untrusted input. Combined with the
  grounding check in `protocol.grounded_proposals` and with the rule that no
  amount used in a calculation ever comes from here, a document that tries to
  give the model instructions cannot change what the pipeline does;
* retries are bounded and only for transient failures. A schema violation is
  retried once with the validation error fed back, then the call is abandoned
  and the document goes to a human;
* token usage and an estimated cost are recorded per call, in the units the
  provider reports, so cost is observable before it is a surprise.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from iep.domain.enums import DocumentKind
from iep.semantic.protocol import (
    SemanticOutcome,
    SemanticProposal,
    SemanticProviderError,
    SemanticRequest,
    SemanticResult,
    SemanticUsage,
    grounded_proposals,
    hash_config,
)

VERSION = "anthropic-messages/1.0.0"

PROMPT_DIR = Path(__file__).parent / "prompts"

log = logging.getLogger(__name__)

# Published list prices at the time of writing, in USD per million tokens.
# They are configuration, not a promise: `docs/business-impact.md` explains
# that every cost figure in this repository is a parameterised estimate.
PRICE_USD_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}
DEFAULT_PRICE = (Decimal("5.00"), Decimal("25.00"))
USD_TO_EUR = Decimal("0.92")


class LlmProposal(BaseModel):
    """The per-field shape the model is constrained to emit."""

    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(description="One of the requested field paths, copied exactly.")
    value: str = Field(description="The value as it appears in the document.")
    evidence_quote: str = Field(
        description=(
            "The exact substring of the document the value was read from. "
            "It must appear verbatim in the document text."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0)


class LlmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_kind: DocumentKind
    kind_confidence: float = Field(ge=0.0, le=1.0)
    proposals: list[LlmProposal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MessagesClient(Protocol):
    """The slice of the SDK this adapter uses.

    Depending on the shape rather than the concrete client is what lets the
    tests drive the adapter with a double and still exercise every branch:
    schema violation, timeout, rate limit, retry and success.
    """

    def parse(self, **kwargs: Any) -> Any: ...


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


class AnthropicSemanticExtractor:
    name = "anthropic"
    version = VERSION

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_attempts: int,
        max_output_tokens: int,
        base_url: str | None = None,
        client: MessagesClient | None = None,
    ) -> None:
        if client is None and not api_key:
            raise SemanticProviderError(
                "the hosted semantic provider requires IEP_LLM_API_KEY to be set",
                retryable=False,
            )
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.max_output_tokens = max_output_tokens
        self.system_prompt = load_prompt("extraction_system.txt")
        self.user_template = load_prompt("extraction_user.txt")
        self._client: Any = client
        self._api_key = api_key
        self._base_url = base_url

    def _messages(self) -> MessagesClient:
        client = self._client
        if client is None:
            import anthropic

            sdk = anthropic.Anthropic(
                api_key=self._api_key,
                base_url=self._base_url,
                timeout=self.timeout_seconds,
                # Retries are handled here so that a schema violation and a
                # transport failure are not conflated in the attempt count.
                max_retries=0,
            )
            client = sdk.messages
            self._client = client
        return client

    def config_hash(self) -> str:
        return hash_config(
            {
                "version": VERSION,
                "model": self.model,
                "max_output_tokens": self.max_output_tokens,
                "system_prompt": self.system_prompt,
                "user_template": self.user_template,
                "response_schema": LlmResponse.model_json_schema(),
            }
        )

    def run(self, request: SemanticRequest) -> SemanticOutcome:
        import anthropic

        field_lines = "\n".join(
            f"- {spec.field_path} ({spec.value_type}): {spec.description}"
            for spec in request.fields
        )
        kinds = ", ".join(str(kind) for kind in request.candidate_kinds)
        user_message = self.user_template.format(
            field_lines=field_lines,
            candidate_kinds=kinds,
            document_text=request.truncated_text,
        )

        attempts = 0
        rejected = 0
        input_tokens = 0
        output_tokens = 0
        last_error: Exception | None = None
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        while attempts < self.max_attempts:
            attempts += 1
            started = time.monotonic()
            try:
                response = self._messages().parse(
                    model=self.model,
                    max_tokens=self.max_output_tokens,
                    system=self.system_prompt,
                    messages=messages,
                    output_format=LlmResponse,
                )
            except anthropic.APITimeoutError as exc:
                last_error = exc
                log.warning("llm_timeout", extra={"attempt": attempts, "model": self.model})
                continue
            except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
                last_error = exc
                log.warning(
                    "llm_transient_error",
                    extra={"attempt": attempts, "error_type": type(exc).__name__},
                )
                continue
            except anthropic.APIStatusError as exc:
                if exc.status_code >= 500:
                    last_error = exc
                    continue
                # A 4xx is a request problem: retrying sends the same request.
                raise SemanticProviderError(
                    f"provider rejected the request ({exc.status_code})", retryable=False
                ) from exc
            except ValidationError as exc:
                # The SDK validates the structured output itself and raises
                # rather than handing back an unparsed result, so a reply that
                # does not fit the schema arrives here. Without this branch a
                # malformed response escaped as a bare pydantic error and took
                # the pipeline down instead of being retried and then abandoned.
                rejected += 1
                last_error = exc
                log.warning(
                    "llm_schema_violation",
                    extra={"attempt": attempts, "errors": exc.error_count()},
                )
                messages = self._retry_messages(user_message, str(exc)[:800])
                continue

            usage = getattr(response, "usage", None)
            input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

            parsed = getattr(response, "parsed_output", None)
            if parsed is None:
                rejected += 1
                last_error = SemanticProviderError(
                    "provider returned no parsable structured output", retryable=True
                )
                messages = self._retry_messages(user_message, "the response was not valid JSON")
                continue

            try:
                validated = (
                    parsed
                    if isinstance(parsed, LlmResponse)
                    else LlmResponse.model_validate(parsed)
                )
            except ValidationError as exc:
                rejected += 1
                last_error = exc
                messages = self._retry_messages(user_message, str(exc)[:800])
                continue

            elapsed = time.monotonic() - started
            log.info(
                "llm_call_completed",
                extra={
                    "model": self.model,
                    "attempts": attempts,
                    "duration_seconds": round(elapsed, 3),
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            )
            return self._finish(request, validated, attempts, rejected, input_tokens, output_tokens)

        raise SemanticProviderError(
            f"provider did not return a usable response after {attempts} attempts: {last_error}",
            retryable=True,
        )

    def _retry_messages(self, user_message: str, problem: str | None) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": user_message},
            {
                "role": "user",
                "content": (
                    "The previous response could not be used: "
                    f"{problem or 'it did not match the required schema'}. "
                    "Reply again with the required structure and nothing else."
                ),
            },
        ]

    def _finish(
        self,
        request: SemanticRequest,
        validated: LlmResponse,
        attempts: int,
        rejected: int,
        input_tokens: int,
        output_tokens: int,
    ) -> SemanticOutcome:
        result = SemanticResult(
            document_kind=validated.document_kind,
            kind_confidence=validated.kind_confidence,
            proposals=tuple(
                SemanticProposal(
                    field_path=p.field_path,
                    value=p.value,
                    evidence_quote=p.evidence_quote,
                    confidence=p.confidence,
                )
                for p in validated.proposals
            ),
            notes=tuple(validated.notes),
        )
        kept, ungrounded = grounded_proposals(
            result,
            request.text,
            frozenset(spec.field_path for spec in request.fields),
        )
        if ungrounded:
            log.warning(
                "llm_ungrounded_proposals_discarded",
                extra={"count": len(ungrounded), "document_id": request.document_id},
            )

        return SemanticOutcome(
            result=result.model_copy(update={"proposals": kept}),
            usage=SemanticUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attempts=attempts,
                rejected_responses=rejected + len(ungrounded),
                estimated_cost_eur=float(
                    estimate_cost_eur(self.model, input_tokens, output_tokens)
                ),
            ),
            provider=self.name,
            provider_version=self.version,
            config_hash=self.config_hash(),
            warnings=ungrounded,
        )


def estimate_cost_eur(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """Parameterised estimate. Not a measured cost: no call is ever billed here."""
    price_in, price_out = PRICE_USD_PER_MTOK.get(model, DEFAULT_PRICE)
    million = Decimal(1_000_000)
    usd = (Decimal(input_tokens) / million) * price_in + (
        Decimal(output_tokens) / million
    ) * price_out
    return (usd * USD_TO_EUR).quantize(Decimal("0.000001"))
