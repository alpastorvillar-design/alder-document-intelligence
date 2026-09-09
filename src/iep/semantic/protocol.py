"""The seam between deterministic extraction and a language model.

Everything the pipeline asks a model to do goes through `SemanticExtractor`.
Two implementations exist: a deterministic one that is the default and is what
tests and demos run against, and an adapter for a hosted model. Because the
pipeline only knows this protocol, the model is a swappable component rather
than a dependency of the business logic.

Three constraints are enforced on every result, whatever the implementation:

1. the output must validate against the declared schema, or it is discarded;
2. every proposed value must quote text that actually appears in the source
   document, or it is discarded — a model cannot introduce a figure that is
   not on the page;
3. nothing a semantic provider returns is used for arithmetic. Amounts that
   feed a validation rule come from deterministic extraction only. The
   provider's job is classification and locating candidates, not computing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from iep.domain.enums import DocumentKind


class SemanticFieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str
    description: str
    value_type: str = Field(pattern=r"^(text|number|date)$")


class SemanticProposal(BaseModel):
    """One field a provider believes it has found."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=500)
    # The exact substring of the source the value was read from. This is what
    # makes grounding checkable rather than a matter of trust.
    evidence_quote: str = Field(min_length=3, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)


class SemanticResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_kind: DocumentKind
    kind_confidence: float = Field(ge=0.0, le=1.0)
    proposals: tuple[SemanticProposal, ...] = ()
    # Populated by the provider when it wants a human to look, e.g. because the
    # document contradicts itself.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SemanticRequest:
    document_id: str
    text: str
    fields: tuple[SemanticFieldSpec, ...]
    candidate_kinds: tuple[DocumentKind, ...]
    max_text_chars: int = 12000

    @property
    def truncated_text(self) -> str:
        return self.text[: self.max_text_chars]


@dataclass(frozen=True)
class SemanticUsage:
    """What a call cost, in units the provider reports.

    Recorded even for the deterministic provider (where it is zero) so the
    accounting path is exercised in tests rather than only in production.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1
    rejected_responses: int = 0
    estimated_cost_eur: float = 0.0


@dataclass(frozen=True)
class SemanticOutcome:
    result: SemanticResult
    usage: SemanticUsage
    provider: str
    provider_version: str
    config_hash: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


class SemanticExtractor(Protocol):
    name: str
    version: str

    def config_hash(self) -> str: ...

    def run(self, request: SemanticRequest) -> SemanticOutcome: ...


class SemanticProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


_WHITESPACE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def grounded_proposals(
    result: SemanticResult,
    source_text: str,
    allowed_fields: frozenset[str] | None = None,
) -> tuple[tuple[SemanticProposal, ...], tuple[str, ...]]:
    """Drop proposals that are unrequested or not grounded in quoted source text.

    This is the guard that makes an invented figure unusable: a value the
    document does not contain cannot be quoted from it, so it never becomes an
    extraction.
    """
    haystack = _normalise(source_text)
    kept: list[SemanticProposal] = []
    rejected: list[str] = []
    for proposal in result.proposals:
        quote = _normalise(proposal.evidence_quote)
        value = _normalise(proposal.value)
        if allowed_fields is not None and proposal.field_path not in allowed_fields:
            rejected.append(f"{proposal.field_path}: field was not requested")
        elif quote in haystack and value in quote:
            kept.append(proposal)
        else:
            rejected.append(f"{proposal.field_path}: value is not grounded in its source quotation")
    return tuple(kept), tuple(rejected)


def hash_config(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
