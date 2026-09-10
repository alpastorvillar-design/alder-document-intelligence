"""Closed vocabularies.

These values are persisted, so they are string enums with stable spellings.
Renaming a member is a migration, not a refactor.
"""

from __future__ import annotations

from enum import StrEnum


class DossierStatus(StrEnum):
    DRAFT = "DRAFT"
    INGESTED = "INGESTED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class DocumentStatus(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTED = "EXTRACTED"
    UNSUPPORTED = "UNSUPPORTED"
    CORRUPT = "CORRUPT"
    FAILED = "FAILED"


class DocumentKind(StrEnum):
    """Result of classification, not of the upload."""

    TECHNICAL_REPORT = "TECHNICAL_REPORT"
    EXPENSE_INVOICE = "EXPENSE_INVOICE"
    TIMESHEET = "TIMESHEET"
    UNKNOWN = "UNKNOWN"


class MediaKind(StrEnum):
    """What the bytes actually are, decided by signature and parser."""

    PDF = "PDF"
    PNG = "PNG"
    JPEG = "JPEG"
    XLSX = "XLSX"
    JSON = "JSON"
    HTML = "HTML"
    UNSUPPORTED = "UNSUPPORTED"


class SourceKind(StrEnum):
    UPLOAD = "UPLOAD"
    REGISTRY_API = "REGISTRY_API"
    PUBLIC_PAGE = "PUBLIC_PAGE"


class ExtractionMethod(StrEnum):
    PDF_TEXT = "PDF_TEXT"
    OCR_TESSERACT = "OCR_TESSERACT"
    EXCEL_CELL = "EXCEL_CELL"
    HTTP_API = "HTTP_API"
    HTML_SELECTOR = "HTML_SELECTOR"
    SEMANTIC_DETERMINISTIC = "SEMANTIC_DETERMINISTIC"
    SEMANTIC_LLM = "SEMANTIC_LLM"
    AGGREGATED = "AGGREGATED"
    HUMAN = "HUMAN"


class LocatorKind(StrEnum):
    PDF_PAGE = "PDF_PAGE"
    OCR_WORD_BOX = "OCR_WORD_BOX"
    EXCEL_CELL = "EXCEL_CELL"
    API_FIELD = "API_FIELD"
    HTML_SELECTOR = "HTML_SELECTOR"
    DERIVED = "DERIVED"


class FieldStatus(StrEnum):
    EXTRACTED = "EXTRACTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    ACCEPTED = "ACCEPTED"
    DISMISSED = "DISMISSED"
    RESOLVED = "RESOLVED"


class DecisionAction(StrEnum):
    CORRECT_FIELD = "CORRECT_FIELD"
    CONFIRM_FIELD = "CONFIRM_FIELD"
    ACCEPT_FINDING = "ACCEPT_FINDING"
    DISMISS_FINDING = "DISMISS_FINDING"
    APPROVE_DOSSIER = "APPROVE_DOSSIER"
    REJECT_DOSSIER = "REJECT_DOSSIER"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class JobType(StrEnum):
    PROCESS_DOSSIER = "PROCESS_DOSSIER"


class AuditAction(StrEnum):
    DOSSIER_CREATED = "DOSSIER_CREATED"
    DOCUMENT_RECEIVED = "DOCUMENT_RECEIVED"
    DOCUMENT_REJECTED = "DOCUMENT_REJECTED"
    DOCUMENT_DUPLICATE = "DOCUMENT_DUPLICATE"
    PROCESSING_ENQUEUED = "PROCESSING_ENQUEUED"
    PROCESSING_STARTED = "PROCESSING_STARTED"
    PROCESSING_FINISHED = "PROCESSING_FINISHED"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    JOB_RECLAIMED = "JOB_RECLAIMED"
    STATE_CHANGED = "STATE_CHANGED"
    FIELD_CORRECTED = "FIELD_CORRECTED"
    FIELD_CONFIRMED = "FIELD_CONFIRMED"
    FINDING_ACCEPTED = "FINDING_ACCEPTED"
    FINDING_DISMISSED = "FINDING_DISMISSED"
    DOSSIER_APPROVED = "DOSSIER_APPROVED"
    DOSSIER_REJECTED = "DOSSIER_REJECTED"
    REPORT_GENERATED = "REPORT_GENERATED"
    # A read-only grounded answer. It changes nothing, which is exactly why it
    # has to be recorded: otherwise the one thing a language model touched in
    # this system leaves no trace, and the call budget has nothing to count.
    EVIDENCE_QUESTION_ANSWERED = "EVIDENCE_QUESTION_ANSWERED"
