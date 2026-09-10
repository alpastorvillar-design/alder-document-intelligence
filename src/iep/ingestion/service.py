"""Ingestion: turn bytes into a document row with provenance.

The order of checks matters and is asserted by tests: size first, then
signature, then parser, then digest, then storage, then the database row.
Nothing is stored until it is known to be a supported, openable file, and
nothing is trusted because of its name.
"""

from __future__ import annotations

import io
import uuid
import warnings
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.errors import InvalidStateTransitionError
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

# Storage key for a submission that was refused: recorded, never stored.
NOT_STORED = "not-stored"

# A dossier under review or already approved does not take new documents:
# late evidence would change what a reviewer already looked at.
ACCEPTS_DOCUMENTS = frozenset(
    {
        DossierStatus.DRAFT,
        DossierStatus.INGESTED,
        DossierStatus.REJECTED,
        DossierStatus.FAILED,
    }
)


def accepts_documents(dossier: Dossier) -> bool:
    return DossierStatus(dossier.status) in ACCEPTS_DOCUMENTS


@dataclass(frozen=True)
class IngestResult:
    document: Document
    duplicate_of: uuid.UUID | None

    @property
    def is_duplicate(self) -> bool:
        return self.duplicate_of is not None


class IngestionRejectedError(Exception):
    def __init__(
        self, reason: str, *, status: DocumentStatus, document_id: uuid.UUID | None = None
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.document_id = document_id


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
    if not accepts_documents(dossier):
        raise InvalidStateTransitionError(
            "Documents cannot be added while the dossier is being processed or after approval.",
            {"status": str(dossier.status)},
        )
    display_name = safe_display_name(filename)

    if len(data) > settings.max_upload_bytes:
        raise IngestionRejectedError(
            f"el fichero ocupa {len(data)} bytes y el límite es {settings.max_upload_bytes} bytes",
            status=DocumentStatus.UNSUPPORTED,
        )
    if not data:
        raise IngestionRejectedError("el fichero está vacío", status=DocumentStatus.CORRUPT)

    try:
        media_kind = sniff(data, max_decompressed_bytes=settings.max_decompressed_bytes)
    except UnsupportedMediaError as exc:
        raise _reject(
            session, dossier, display_name, data, exc.reason, DocumentStatus.UNSUPPORTED
        ) from exc
    except CorruptFileError as exc:
        raise _reject(
            session, dossier, display_name, data, exc.reason, DocumentStatus.CORRUPT
        ) from exc

    try:
        page_count = _validate_content(media_kind, data, settings)
    except IngestionRejectedError as exc:
        raise _reject(session, dossier, display_name, data, exc.reason, exc.status) from exc

    digest = content_digest(data)

    existing = session.execute(
        select(Document).where(Document.dossier_id == dossier.id, Document.content_sha256 == digest)
    ).scalar_one_or_none()
    if existing is not None:
        # Re-uploading the same bytes is a no-op, not an error: an automation
        # retrying a POST must not create a second copy.
        if display_name != existing.original_filename:
            # A second *name* for the same bytes is worth telling a reviewer
            # about. The same name again is an idempotent retry and is not.
            known = list(existing.alternate_filenames or [])
            if display_name not in known:
                existing.alternate_filenames = [*known, display_name]
        audit.record(
            session,
            action=AuditAction.DOCUMENT_DUPLICATE,
            dossier_id=dossier.id,
            payload={
                "document_id": str(existing.id),
                "content_sha256": digest,
                "filename": display_name,
                "recorded_as_alternate": display_name != existing.original_filename,
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

    if dossier.status in (
        DossierStatus.DRAFT,
        DossierStatus.INGESTED,
        DossierStatus.REJECTED,
        DossierStatus.FAILED,
    ):
        dossier.status = DossierStatus.INGESTED

    return IngestResult(document=document, duplicate_of=None)


def _reject(
    session: Session,
    dossier: Dossier,
    display_name: str,
    data: bytes,
    reason: str,
    status: DocumentStatus,
) -> IngestionRejectedError:
    """Record what was submitted and why it was refused, without storing it.

    The bytes are not written to the object store - they failed the checks that
    decide whether they are safe to keep - but the submission is still part of
    the dossier's history, and a validation rule can report it to a reviewer
    instead of the file silently not existing.
    """
    digest = content_digest(data)
    existing = session.execute(
        select(Document).where(Document.dossier_id == dossier.id, Document.content_sha256 == digest)
    ).scalar_one_or_none()
    if existing is None:
        document = Document(
            id=uuid.uuid4(),
            dossier_id=dossier.id,
            original_filename=display_name,
            declared_media_type=None,
            media_kind=MediaKind.UNSUPPORTED,
            document_kind=DocumentKind.UNKNOWN,
            status=status,
            source_kind=SourceKind.UPLOAD,
            source_detail=None,
            size_bytes=len(data),
            content_sha256=digest,
            storage_key=NOT_STORED,
            page_count=None,
            rejection_reason=reason,
        )
        session.add(document)
        session.flush()
    else:
        document = existing

    audit.record(
        session,
        action=AuditAction.DOCUMENT_REJECTED,
        dossier_id=dossier.id,
        payload={
            "document_id": str(document.id),
            "filename": display_name,
            "reason": reason,
            "status": str(status),
            "content_sha256": digest,
            "bytes_stored": False,
        },
    )
    return IngestionRejectedError(reason, status=status, document_id=document.id)


def _validate_content(media_kind: MediaKind, data: bytes, settings: Settings) -> int | None:
    """Open parser-backed formats before storage and enforce expansion ceilings."""
    if media_kind in (MediaKind.PNG, MediaKind.JPEG):
        from PIL import Image, UnidentifiedImageError

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    pixels = image.width * image.height
                    if pixels > settings.max_image_pixels:
                        raise IngestionRejectedError(
                            f"la imagen tiene {pixels} píxeles y el límite es "
                            f"{settings.max_image_pixels}",
                            status=DocumentStatus.UNSUPPORTED,
                        )
                    image.verify()
        except IngestionRejectedError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise IngestionRejectedError(
                "la imagen supera el límite seguro de descompresión",
                status=DocumentStatus.UNSUPPORTED,
            ) from exc
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise IngestionRejectedError(
                f"la imagen no se pudo abrir: {type(exc).__name__}",
                status=DocumentStatus.CORRUPT,
            ) from exc
        return None

    if media_kind is not MediaKind.PDF:
        return None
    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise IngestionRejectedError(
                    "el PDF está cifrado", status=DocumentStatus.UNSUPPORTED
                )
            pages = doc.page_count
    except IngestionRejectedError:
        raise
    except Exception as exc:  # pymupdf raises a broad family for malformed input
        raise IngestionRejectedError(
            f"el PDF no se pudo abrir: {type(exc).__name__}", status=DocumentStatus.CORRUPT
        ) from exc

    if pages > settings.max_pdf_pages:
        raise IngestionRejectedError(
            f"el PDF tiene {pages} páginas y el límite es {settings.max_pdf_pages}",
            status=DocumentStatus.UNSUPPORTED,
        )
    if pages == 0:
        raise IngestionRejectedError("el PDF no tiene páginas", status=DocumentStatus.CORRUPT)
    return pages
