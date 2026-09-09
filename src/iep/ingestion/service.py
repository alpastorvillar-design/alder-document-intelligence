"""Ingestion: turn bytes into a document row with provenance.

The order of checks matters and is asserted by tests: size first, then
signature, then parser, then digest, then storage, then the database row.
Nothing is stored until it is known to be a supported, openable file, and
nothing is trusted because of its name.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.audit import service as audit
from iep.config import Settings
from iep.db.models import Document, Dossier
from iep.domain.enums import (
    AuditAction,
    DocumentKind,
    DocumentStatus,
    DossierStatus,
    MediaKind,
    SourceKind,
)
from iep.ingestion.sniff import (
    CorruptFileError,
    UnsupportedMediaError,
    guess_from_name,
    sniff,
)
from iep.storage.base import ObjectStore
from iep.storage.local import content_digest

MAX_FILENAME_LENGTH = 255


@dataclass(frozen=True)
class IngestResult:
    document: Document
    duplicate_of: uuid.UUID | None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


class IngestionRejectedError(Exception):
    def __init__(self, reason: str, *, status: DocumentStatus) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def safe_display_name(filename: str) -> str:
    """A caller's filename, kept only for display and provenance.

    It never becomes a path. Separators and control characters are stripped so
    that echoing it into a report or a log line cannot inject structure.
    """
    cleaned = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() and ch not in '<>:"|?*')
    cleaned = cleaned.strip().strip(".")
    return (cleaned or "unnamed")[:MAX_FILENAME_LENGTH]


def ingest_upload(
    session: Session,
    store: ObjectStore,
    settings: Settings,
    *,
    dossier: Dossier,
    filename: str,
    declared_media_type: str | None,
    data: bytes,
    source_kind: SourceKind = SourceKind.UPLOAD,
    source_detail: str | None = None,
) -> IngestResult:
    display_name = safe_display_name(filename)

    if len(data) > settings.max_upload_bytes:
        raise IngestionRejectedError(
            f"payload of {len(data)} bytes exceeds the {settings.max_upload_bytes} byte limit",
            status=DocumentStatus.UNSUPPORTED,
        )
    if not data:
        raise IngestionRejectedError("empty payload", status=DocumentStatus.CORRUPT)

    try:
        media_kind = sniff(data, max_decompressed_bytes=settings.max_decompressed_bytes)
    except UnsupportedMediaError as exc:
        raise IngestionRejectedError(exc.reason, status=DocumentStatus.UNSUPPORTED) from exc
    except CorruptFileError as exc:
        raise IngestionRejectedError(exc.reason, status=DocumentStatus.CORRUPT) from exc

    page_count = _page_count(media_kind, data, settings)

    digest = content_digest(data)

    existing = session.execute(
        select(Document).where(Document.dossier_id == dossier.id, Document.content_sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        # Re-uploading the same bytes is a no-op, not an error: an automation
        # retrying a POST must not create a second copy.
        audit.record(
            session,
            action=AuditAction.DOCUMENT_DUPLICATE,
            dossier_id=dossier.id,
            payload={
                "document_id": str(existing.id),
                "content_sha256": digest,
                "filename": display_name,
            },
        )
        return IngestResult(document=existing, duplicate_of=existing.id)

    storage_key = store.put(digest, data)

    document = Document(
        id=uuid.uuid4(),
        dossier_id=dossier.id,
        original_filename=display_name,
        declared_media_type=(declared_media_type or "")[:120] or None,
        media_kind=media_kind,
        document_kind=DocumentKind.UNKNOWN,
        status=DocumentStatus.RECEIVED,
        source_kind=source_kind,
        source_detail=source_detail,
        size_bytes=len(data),
        content_sha256=digest,
        storage_key=storage_key,
        page_count=page_count,
    )
    session.add(document)
    session.flush()

    audit.record(
        session,
        action=AuditAction.DOCUMENT_RECEIVED,
        dossier_id=dossier.id,
        payload={
            "document_id": str(document.id),
            "filename": display_name,
            "declared_media_type": declared_media_type,
            "detected_media_kind": str(media_kind),
            "declared_extension": guess_from_name(display_name),
            "size_bytes": len(data),
            "content_sha256": digest,
            "page_count": page_count,
            "source_kind": str(source_kind),
        },
    )

    if dossier.status in (DossierStatus.DRAFT, DossierStatus.INGESTED, DossierStatus.REJECTED):
        dossier.status = DossierStatus.INGESTED

    return IngestResult(document=document, duplicate_of=None)


def _page_count(media_kind: MediaKind, data: bytes, settings: Settings) -> int | None:
    """Open PDFs eagerly so a corrupt file is rejected at ingestion, not later."""
    if media_kind is not MediaKind.PDF:
        return None
    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise IngestionRejectedError("encrypted PDF", status=DocumentStatus.UNSUPPORTED)
            pages = doc.page_count
    except IngestionRejectedError:
        raise
    except Exception as exc:  # pymupdf raises a broad family for malformed input
        raise IngestionRejectedError(
            f"PDF could not be opened: {type(exc).__name__}", status=DocumentStatus.CORRUPT
        ) from exc

    if pages > settings.max_pdf_pages:
        raise IngestionRejectedError(
            f"PDF has {pages} pages, above the {settings.max_pdf_pages} page limit",
            status=DocumentStatus.UNSUPPORTED,
        )
    if pages == 0:
        raise IngestionRejectedError("PDF has no pages", status=DocumentStatus.CORRUPT)
    return pages
