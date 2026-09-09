"""Persistence model.

Two decisions are load-bearing and are asserted by tests rather than left to
convention:

* uniqueness lives in the database, not in application checks. Duplicate
  documents, duplicate findings and replayed requests are prevented by
  constraints, so a race loses at commit time instead of producing a second row;
* `audit_events` is append-only. Nothing in the codebase updates or deletes it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from iep.domain.enums import (
    DocumentKind,
    DocumentStatus,
    DossierStatus,
    FieldStatus,
    FindingStatus,
    JobStatus,
    JobType,
    MediaKind,
    SourceKind,
)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


TS = DateTime(timezone=True)


class Dossier(Base):
    __tablename__ = "dossiers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # One dossier per external reference. Re-submitting the same reference is a
    # replay of an existing dossier, never a second one.
    reference: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    claimed_total_eur: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    call_page_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[DossierStatus] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    documents: Mapped[list[Document]] = relationship(back_populates="dossier")

    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_dossier_period_ordered"),
        CheckConstraint("claimed_total_eur >= 0", name="ck_dossier_total_non_negative"),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    declared_media_type: Mapped[str | None] = mapped_column(String(120))
    media_kind: Mapped[MediaKind] = mapped_column(String(16), nullable=False)
    document_kind: Mapped[DocumentKind] = mapped_column(
        String(32), nullable=False, default=DocumentKind.UNKNOWN
    )
    status: Mapped[DocumentStatus] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[SourceKind] = mapped_column(String(16), nullable=False)
    source_detail: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(160), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    dossier: Mapped[Dossier] = relationship(back_populates="documents")

    __table_args__ = (
        # Content-level dedup, scoped to the dossier: the same invoice may
        # legitimately appear in two different claims.
        UniqueConstraint("dossier_id", "content_sha256", name="uq_document_dossier_content"),
        CheckConstraint("size_bytes > 0", name="ck_document_size_positive"),
        Index("ix_documents_dossier", "dossier_id"),
    )


class Extraction(Base):
    __tablename__ = "extractions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE")
    )
    field_path: Mapped[str] = mapped_column(String(200), nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_number: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    value_date: Mapped[date | None] = mapped_column(Date)
    locator: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False)
    status: Mapped[FieldStatus] = mapped_column(String(16), nullable=False)
    original_value_text: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[str | None] = mapped_column(String(120))
    corrected_at: Mapped[datetime | None] = mapped_column(TS)
    correction_reason: Mapped[str | None] = mapped_column(Text)
    # Stable across re-processing of the same inputs. This is what makes replay
    # update a row instead of appending a near-duplicate.
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("dossier_id", "dedup_key", name="uq_extraction_dossier_dedup"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_extraction_confidence"),
        Index("ix_extractions_dossier_field", "dossier_id", "field_path"),
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[FindingStatus] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False, default=dict)
    extraction_ids: Mapped[list[str]] = mapped_column(
        postgresql.JSONB, nullable=False, default=list
    )
    document_ids: Mapped[list[str]] = mapped_column(postgresql.JSONB, nullable=False, default=list)
    # Same rule + same subject = same finding, so re-running validation refreshes
    # rather than accumulates, and a reviewer's resolution survives a re-run.
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    resolved_by: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    resolution_note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("dossier_id", "fingerprint", name="uq_finding_dossier_fingerprint"),
        Index("ix_findings_dossier_status", "dossier_id", "status"),
    )


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True))
    finding_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True))
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_review_decisions_dossier", "dossier_id", "created_at"),)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[JobType] = mapped_column(String(32), nullable=False)
    status: Mapped[JobStatus] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    payload: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    available_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    leased_until: Mapped[datetime | None] = mapped_column(TS)
    leased_by: Mapped[str | None] = mapped_column(String(120))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TS, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_job_idempotency_key"),
        # The claim query filters on exactly these columns.
        Index("ix_jobs_claimable", "status", "available_at"),
    )


class AuditEvent(Base):
    """Append-only. Nothing in this codebase issues UPDATE or DELETE here."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID | None] = mapped_column(postgresql.UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_audit_dossier_created", "dossier_id", "created_at"),)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(160), nullable=False)
    dossier_status: Mapped[str] = mapped_column(String(24), nullable=False)
    finding_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocker_count: Mapped[int] = mapped_column(Integer, nullable=False)
    needs_review_count: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)

    __table_args__ = (Index("ix_reports_dossier_generated", "dossier_id", "generated_at"),)


class IdempotencyRecord(Base):
    """Request-level replay protection for unsafe endpoints."""

    __tablename__ = "idempotency_records"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(120), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), nullable=False)


class DocumentChunk(Base):
    """Text segments with locators, used for evidence lookup.

    This is lexical search over the dossier's own documents. It is not RAG:
    nothing generates text from these rows.
    """

    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    dossier_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True), ForeignKey("dossiers.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB, nullable=False)
    search_vector: Mapped[str] = mapped_column(
        postgresql.TSVECTOR, Computed("to_tsvector('spanish', text)", persisted=True)
    )

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_chunk_document_ordinal"),
        Index("ix_chunks_search", "search_vector", postgresql_using="gin"),
        Index("ix_chunks_dossier", "dossier_id"),
    )
