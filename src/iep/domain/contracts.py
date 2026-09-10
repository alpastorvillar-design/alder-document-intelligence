"""Pydantic contracts for everything that crosses a boundary.

`CONTRACT_VERSION` is stamped onto every extraction that is persisted. When the
shape of an extraction changes, this is bumped and old rows keep saying which
shape they were written under, which is what makes a stored result explainable
a year later.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from iep.domain.enums import (
    DecisionAction,
    DocumentKind,
    DocumentStatus,
    DossierStatus,
    ExtractionMethod,
    FieldStatus,
    FindingStatus,
    JobStatus,
    JobType,
    LocatorKind,
    MediaKind,
    Severity,
    SourceKind,
)

CONTRACT_VERSION = "1.0.0"

DOSSIER_REFERENCE_PATTERN = re.compile(r"^INN-\d{4}-\d{3}$")


class Base(BaseModel):
    # `from_attributes` lets a persistence row be validated straight into the
    # contract, so the API cannot accidentally return a shape the contract
    # does not describe.
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)


# --------------------------------------------------------------------------
# Evidence locators
# --------------------------------------------------------------------------
# A locator answers "where exactly did this value come from?". It is a tagged
# union rather than a bag of optional fields so an invalid combination — an
# Excel cell with a bounding box, say — cannot be constructed at all.


class PdfPageLocator(Base):
    kind: Literal[LocatorKind.PDF_PAGE] = LocatorKind.PDF_PAGE
    page: int = Field(ge=1)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    snippet: str | None = Field(default=None, max_length=500)


class OcrWordBoxLocator(Base):
    kind: Literal[LocatorKind.OCR_WORD_BOX] = LocatorKind.OCR_WORD_BOX
    page: int = Field(ge=1)
    left: int = Field(ge=0)
    top: int = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    word_confidence: float = Field(ge=0.0, le=100.0)
    snippet: str | None = Field(default=None, max_length=500)


class ExcelCellLocator(Base):
    kind: Literal[LocatorKind.EXCEL_CELL] = LocatorKind.EXCEL_CELL
    sheet: str
    cell: str = Field(pattern=r"^[A-Z]{1,3}[0-9]{1,7}$")
    row: int = Field(ge=1)
    column: str = Field(pattern=r"^[A-Z]{1,3}$")


class ApiFieldLocator(Base):
    kind: Literal[LocatorKind.API_FIELD] = LocatorKind.API_FIELD
    endpoint: str
    record_id: str
    json_path: str
    contract_version: str


class HtmlSelectorLocator(Base):
    kind: Literal[LocatorKind.HTML_SELECTOR] = LocatorKind.HTML_SELECTOR
    url: str
    selector: str
    captured_at: datetime
    snippet: str | None = Field(default=None, max_length=500)


class DerivedLocator(Base):
    """A value computed from other extractions, e.g. a sum of invoice totals."""

    kind: Literal[LocatorKind.DERIVED] = LocatorKind.DERIVED
    inputs: tuple[UUID, ...]
    rule: str


EvidenceLocator = Annotated[
    PdfPageLocator
    | OcrWordBoxLocator
    | ExcelCellLocator
    | ApiFieldLocator
    | HtmlSelectorLocator
    | DerivedLocator,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------
# Core entities
# --------------------------------------------------------------------------


class DossierCreate(Base):
    reference: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=300)
    period_start: date
    period_end: date
    claimed_total_eur: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    call_page_url: str | None = None

    @field_validator("reference")
    @classmethod
    def _reference_shape(cls, value: str) -> str:
        value = value.strip().upper()
        if not DOSSIER_REFERENCE_PATTERN.match(value):
            raise ValueError("reference must look like INN-YYYY-NNN")
        return value

    @model_validator(mode="after")
    def _period_ordered(self) -> DossierCreate:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not precede period_start")
        return self


class Dossier(Base):
    id: UUID
    reference: str
    title: str
    period_start: date
    period_end: date
    claimed_total_eur: Decimal
    call_page_url: str | None
    status: DossierStatus
    created_at: datetime
    updated_at: datetime


class Document(Base):
    id: UUID
    dossier_id: UUID
    original_filename: str
    declared_media_type: str | None
    media_kind: MediaKind
    document_kind: DocumentKind
    status: DocumentStatus
    source_kind: SourceKind
    source_detail: str | None
    size_bytes: int
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    storage_key: str
    page_count: int | None
    received_at: datetime
    rejection_reason: str | None = None
    alternate_filenames: tuple[str, ...] = ()


class Extraction(Base):
    """One field, one value, one place it came from."""

    id: UUID
    dossier_id: UUID
    document_id: UUID | None
    field_path: str = Field(min_length=1, max_length=200)
    value_text: str | None
    value_number: Decimal | None
    value_date: date | None
    locator: EvidenceLocator
    method: ExtractionMethod
    extractor_version: str
    contract_version: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: FieldStatus
    original_value_text: str | None = None
    corrected_by: str | None = None
    corrected_at: datetime | None = None
    correction_reason: str | None = None
    revision: int = Field(ge=0)

    @property
    def display_value(self) -> str:
        if self.value_number is not None:
            return f"{self.value_number}"
        if self.value_date is not None:
            return self.value_date.isoformat()
        return self.value_text or ""


class ValidationFinding(Base):
    id: UUID
    dossier_id: UUID
    rule_id: str = Field(min_length=1, max_length=64)
    rule_version: str
    severity: Severity
    status: FindingStatus
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    extraction_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()
    fingerprint: str
    created_at: datetime
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_note: str | None = None


class ReviewDecision(Base):
    id: UUID
    dossier_id: UUID
    action: DecisionAction
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    extraction_id: UUID | None = None
    finding_id: UUID | None = None
    new_value: str | None = None
    created_at: datetime


class ProcessingJob(Base):
    id: UUID
    dossier_id: UUID
    job_type: JobType
    status: JobStatus
    attempts: int
    max_attempts: int
    idempotency_key: str
    available_at: datetime
    leased_until: datetime | None
    leased_by: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class AuditEvent(Base):
    id: UUID
    dossier_id: UUID | None
    action: str
    actor: str
    correlation_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class DossierReport(Base):
    id: UUID
    dossier_id: UUID
    content_sha256: str
    storage_key: str
    dossier_status: DossierStatus
    finding_count: int
    blocker_count: int
    needs_review_count: int
    generated_at: datetime


# --------------------------------------------------------------------------
# Request / response payloads
# --------------------------------------------------------------------------


class ApiError(BaseModel):
    """The only error shape a client ever sees. No stack traces, ever."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str
    correlation_id: str
    detail: dict[str, Any] | None = None


class ReviewCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    new_value: str = Field(min_length=1, max_length=500)
    expected_revision: int = Field(ge=0)


class ReviewConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: int = Field(ge=0)


class FindingResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)
    accept: bool


class DossierDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=1000)


class ProcessingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Absent means "use whatever the deployment is configured with". Present
    # means an operator is deliberately overriding it for one run.
    semantic_provider: Literal["deterministic", "llm"] | None = None


class RagQuestion(BaseModel):
    """A read-only grounded question over one dossier's indexed evidence."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=2, max_length=500)
    retrieval_mode: Literal["lexical", "vector", "hybrid"] = "hybrid"
    top_k: int = Field(default=5, ge=1, le=10)


class RagCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^E[1-9][0-9]*$")
    document: str
    document_id: UUID
    ordinal: int = Field(ge=0)
    text: str
    locator: dict[str, Any]
    where: str


class RagAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    sufficient_evidence: bool
    citations: list[RagCitation]
    # Retrieved segments withheld from the generator because their document is
    # flagged as carrying instructions aimed at an automated reader. Reported
    # rather than swallowed: a caller cannot audit an exclusion it is not told
    # about.
    withheld_hostile_segments: int = Field(default=0, ge=0)
    retrieval_mode: Literal["lexical", "vector", "hybrid"]
    generation_provider: str
    generation_model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    prompt_version: str
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RagStatus(BaseModel):
    """What the answer box is allowed to offer, and why.

    The screen asks for this instead of deciding for itself: a panel that
    offers a model the process cannot reach produces a failure the reviewer
    has to interpret, and one that hides the option teaches nothing about
    where a model fits.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    provider: Literal["disabled", "openai", "cli"]
    # Which assistant CLI is configured, and whether this process can launch
    # it. Both matter: the API runs in a container by default and the CLI is
    # installed on the host.
    cli_tool: str | None = None
    cli_available: bool = False
    available_cli_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    retrieval_modes: list[Literal["lexical", "vector", "hybrid"]] = Field(default_factory=list)
    embedding_provider: str
    # Why it is off, in the words the screen shows. Empty when it is on.
    unavailable_reason: str = ""
    budget_used: int = Field(ge=0)
    budget_ceiling: int = Field(ge=0)
    budget_stop_at: int = Field(ge=0)
    budget_window_days: int = Field(ge=1)
    budget_exhausted: bool = False
