"""What an extractor produces.

A candidate is a value plus the evidence for it. Extractors never write to the
database and never decide anything: they say "this text, at this place, with
this confidence", and the pipeline decides what to do with that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from iep.domain.contracts import EvidenceLocator
from iep.domain.enums import ExtractionMethod


@dataclass(frozen=True)
class FieldCandidate:
    field_path: str
    locator: EvidenceLocator
    method: ExtractionMethod
    extractor_version: str
    confidence: float
    value_text: str | None = None
    value_number: Decimal | None = None
    value_date: date | None = None
    # Free-form provenance that is useful in review but is not the value
    # itself, e.g. the raw OCR string a number was parsed from.
    context: dict[str, Any] = field(default_factory=dict)

    def with_confidence(self, confidence: float) -> FieldCandidate:
        return FieldCandidate(
            field_path=self.field_path,
            locator=self.locator,
            method=self.method,
            extractor_version=self.extractor_version,
            confidence=confidence,
            value_text=self.value_text,
            value_number=self.value_number,
            value_date=self.value_date,
            context=self.context,
        )


@dataclass(frozen=True)
class TextChunk:
    """A retrievable segment of a document, with the locator kept."""

    ordinal: int
    text: str
    locator: EvidenceLocator


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    chunks: tuple[TextChunk, ...]
    candidates: tuple[FieldCandidate, ...]
    # Mean per-word OCR confidence on a 0-1 scale, or None when the document
    # did not go through OCR at all.
    ocr_confidence: float | None = None
    warnings: tuple[str, ...] = ()


class ExtractionError(Exception):
    """Raised when a document cannot be read at all."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
