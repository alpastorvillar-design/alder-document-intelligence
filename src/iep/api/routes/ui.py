"""The review screen.

Server-rendered HTML plus a few fetch calls to the same JSON API an automation
would use. There is no build step and no second implementation of the review
rules: the page can only do what the API allows, which is the point.

The interface is Spanish because the domain, the documents and the people who
would use it are. Route paths, field paths and rule ids stay in English: they
are identifiers, and a caller that hard-codes one should not break because a
label changed.

Five screens, in the order a dossier moves through them: the queue, a new
dossier with its uploads, the progress of the run, the review itself, and the
evidence behind one value.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from iep.api import evidence as evidence_view
from iep.api import vocabulary as vocab
from iep.api.deps import db_session, object_store, require_api_key
from iep.api.errors import NotFoundError
from iep.db.models import Document, Dossier, Extraction, Finding, ProcessingJob
from iep.domain.enums import DocumentStatus, FieldStatus, FindingStatus, Severity
from iep.dossiers import service as dossiers
from iep.storage.base import ObjectNotFoundError, ObjectStore

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["money"] = vocab.money
_env.filters["fecha"] = vocab.spanish_date
_env.globals.update(
    dossier_status=vocab.DOSSIER_STATUS,
    document_status=vocab.DOCUMENT_STATUS,
    field_status=vocab.FIELD_STATUS,
    finding_status=vocab.FINDING_STATUS,
    severity_label=vocab.SEVERITY,
    job_status=vocab.JOB_STATUS,
    document_kind=vocab.DOCUMENT_KIND,
    media_kind=vocab.MEDIA_KIND,
    locator_kind=vocab.LOCATOR_KIND,
    rule=vocab.rule,
    field_label=vocab.field_label,
    locator_summary=vocab.locator_summary,
    detail_rows=vocab.detail_rows,
)

router = APIRouter(prefix="/ui", tags=["ui"], dependencies=[Depends(require_api_key)])


def _page(name: str, **context: Any) -> Response:
    html = _env.get_template(name).render(**context)
    return Response(content=html, media_type="text/html; charset=utf-8")


# --------------------------------------------------------------------------
# 1. The queue
# --------------------------------------------------------------------------


@router.get("", include_in_schema=False)
def index_redirect() -> RedirectResponse:
    """`/ui` is a natural thing to type; send it to the queue rather than 404."""
    return RedirectResponse(url="/ui/dossiers", status_code=307)


@router.get("/dossiers", response_class=Response, summary="The review queue (start here)")
def queue(session: Session = Depends(db_session)) -> Response:
    """The dossiers waiting for a decision, newest first, with what each is holding.

    Open `http://127.0.0.1:8000/ui/dossiers` in a browser. This is the screen a
    reviewer works from; the JSON API underneath it is what an automation uses.
    """
    rows = list(
        session.execute(select(Dossier).order_by(Dossier.created_at.desc()).limit(50)).scalars()
    )
    ids = [row.id for row in rows]
    return _page(
        "queue.html",
        dossiers=rows,
        finding_counts=_finding_counts(session, ids),
        document_counts=_document_counts(session, ids),
    )


# --------------------------------------------------------------------------
# 2. A new dossier
#
# Declared before `/dossiers/{dossier_id}`: FastAPI matches in declaration
# order, and "new" is not a UUID.
# --------------------------------------------------------------------------


@router.get(
    "/dossiers/new",
    response_class=Response,
    summary="Open a dossier and drop its documents in",
)
def new_dossier() -> Response:
    """The intake screen: the claim's own data, then the files that support it.

    The page uses `POST /dossiers`, then one `POST /dossiers/{id}/documents` per
    file, then `POST /dossiers/{id}/process` - the same three calls an
    integration would make, which is why a refusal here looks exactly like a
    refusal there.
    """
    return _page("new.html")


# --------------------------------------------------------------------------
# 3. Progress
# --------------------------------------------------------------------------


@router.get(
    "/dossiers/{dossier_id}/progress",
    response_class=Response,
    summary="Watch the run while the worker does it",
)
def progress(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    """Polls the job until it finishes, then goes to the review screen.

    The stages shown are the pipeline's own: capturing external sources,
    reading documents, classifying, aggregating, validating.
    """
    dossier = dossiers.get(session, dossier_id)
    job = session.execute(
        select(ProcessingJob)
        .where(ProcessingJob.dossier_id == dossier_id)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _page("progress.html", dossier=dossier, job=job)


# --------------------------------------------------------------------------
# 4. Review
# --------------------------------------------------------------------------


@router.get(
    "/dossiers/{dossier_id}",
    response_class=Response,
    summary="Review one dossier: findings, fields, evidence",
)
def review_view(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    """What the rules concluded, then every field with a way back to its source.

    The buttons post to the `review` endpoints, so anything done here is
    recorded with an actor and a reason exactly as an API call would be.
    Approval is refused while a blocking finding is open.
    """
    dossier = dossiers.get(session, dossier_id)

    documents = list(
        session.execute(
            select(Document)
            .where(Document.dossier_id == dossier_id)
            .order_by(Document.received_at.asc())
        ).scalars()
    )
    findings = list(
        session.execute(
            select(Finding)
            .where(Finding.dossier_id == dossier_id)
            .order_by(Finding.severity.asc(), Finding.rule_id.asc())
        ).scalars()
    )
    extractions = list(
        session.execute(
            select(Extraction)
            .where(Extraction.dossier_id == dossier_id)
            .order_by(Extraction.field_path.asc())
        ).scalars()
    )

    open_findings = [f for f in findings if f.status == FindingStatus.OPEN]
    blockers = [
        f
        for f in findings
        if f.severity == Severity.BLOCKER
        and f.status in (FindingStatus.OPEN, FindingStatus.ACCEPTED)
    ]
    needs_review = [e for e in extractions if e.status == FieldStatus.NEEDS_REVIEW]
    refused = [
        d for d in documents if d.status in (DocumentStatus.UNSUPPORTED, DocumentStatus.CORRUPT)
    ]

    return _page(
        "review.html",
        dossier=dossier,
        documents=documents,
        document_names={row.id: row.original_filename for row in documents},
        refused=refused,
        accepted_count=len(documents) - len(refused),
        findings=findings,
        open_findings=open_findings,
        blockers=blockers,
        by_severity=[
            (level, [f for f in findings if f.severity == level])
            for level in (Severity.BLOCKER, Severity.WARNING, Severity.INFO)
        ],
        extractions=extractions,
        needs_review=needs_review,
        grouped=_group_extractions(extractions),
        # A finding names the extractions and documents it points at. Resolving
        # them here is what lets the card offer a way straight to the evidence
        # instead of describing it.
        extraction_by_id={str(row.id): row for row in extractions},
        can_approve=not blockers and not needs_review,
    )


# --------------------------------------------------------------------------
# 5. Evidence
# --------------------------------------------------------------------------


@router.get(
    "/evidence/{extraction_id}",
    response_class=Response,
    summary="Show the source of one value, with the place marked",
)
def evidence(
    extraction_id: uuid.UUID,
    session: Session = Depends(db_session),
    store: ObjectStore = Depends(object_store),
) -> Response:
    """The document this value was read from, with a box around the reading.

    This is the locator walked backwards. For a scan it is the image with the
    words the engine read outlined; for a PDF it is the rasterised page with
    the quoted text outlined; for a workbook it is the cell with its
    neighbours; for a captured page it is the matched fragment and the selector.
    """
    extraction = session.get(Extraction, extraction_id)
    if extraction is None:
        raise NotFoundError("No hay ninguna extraccion con ese identificador.")

    document = session.get(Document, extraction.document_id) if extraction.document_id else None
    view = evidence_view.build(
        locator=dict(extraction.locator or {}),
        media_kind=str(document.media_kind) if document else None,
        data=_document_bytes(store, document),
    )
    inputs = (
        list(session.execute(select(Extraction).where(Extraction.id.in_(view.inputs))).scalars())
        if view.inputs
        else []
    )
    return _page(
        "evidence.html",
        extraction=extraction,
        document=document,
        dossier=dossiers.get(session, extraction.dossier_id),
        view=view,
        inputs=inputs,
    )


@router.get(
    "/evidence/{extraction_id}/image",
    response_class=Response,
    include_in_schema=False,
)
def evidence_image(
    extraction_id: uuid.UUID,
    session: Session = Depends(db_session),
    store: ObjectStore = Depends(object_store),
) -> Response:
    """The page behind an evidence view, as an image the browser can draw on."""
    extraction = session.get(Extraction, extraction_id)
    if extraction is None:
        raise NotFoundError("No hay ninguna extraccion con ese identificador.")
    document = session.get(Document, extraction.document_id) if extraction.document_id else None
    data = _document_bytes(store, document)
    if data is None or document is None:
        raise NotFoundError("Este valor no tiene un documento con contenido almacenado.")

    locator = dict(extraction.locator or {})
    rendered = evidence_view.render_page_png(
        data, str(document.media_kind), int(locator.get("page") or 1)
    )
    if rendered is None:
        raise NotFoundError("No se pudo preparar la imagen de esta pagina.")

    media_type = "image/jpeg" if _is_jpeg(rendered) else "image/png"
    return Response(
        content=rendered,
        media_type=media_type,
        # The bytes are addressed by content digest, so they never change.
        headers={"Cache-Control": "private, max-age=3600"},
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _document_bytes(store: ObjectStore, document: Document | None) -> bytes | None:
    if document is None or not document.storage_key:
        return None
    try:
        return store.get(document.storage_key)
    except (ObjectNotFoundError, OSError):
        return None


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


def _finding_counts(
    session: Session, dossier_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    """Open findings and open-or-accepted blockers, per dossier, in one query."""
    if not dossier_ids:
        return {}
    rows = session.execute(
        select(Finding.dossier_id, Finding.severity, Finding.status, func.count())
        .where(Finding.dossier_id.in_(dossier_ids))
        .group_by(Finding.dossier_id, Finding.severity, Finding.status)
    ).all()
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for dossier_id, severity, status, count in rows:
        bucket = counts.setdefault(dossier_id, {"open": 0, "blockers": 0})
        if status == FindingStatus.OPEN:
            bucket["open"] += count
        if severity == Severity.BLOCKER and status in (
            FindingStatus.OPEN,
            FindingStatus.ACCEPTED,
        ):
            bucket["blockers"] += count
    return counts


def _document_counts(
    session: Session, dossier_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, int]]:
    if not dossier_ids:
        return {}
    rows = session.execute(
        select(Document.dossier_id, Document.status, func.count())
        .where(Document.dossier_id.in_(dossier_ids))
        .group_by(Document.dossier_id, Document.status)
    ).all()
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for dossier_id, status, count in rows:
        bucket = counts.setdefault(dossier_id, {"accepted": 0, "refused": 0})
        if status in (DocumentStatus.UNSUPPORTED, DocumentStatus.CORRUPT):
            bucket["refused"] += count
        else:
            bucket["accepted"] += count
    return counts


def _group_extractions(extractions: list[Extraction]) -> list[tuple[str, str, list[Extraction]]]:
    """Fields in the order a reviewer would read them: by where they came from."""
    buckets: dict[str, list[Extraction]] = {}
    for row in extractions:
        buckets.setdefault(vocab.group_of(row.field_path), []).append(row)
    ordered: list[tuple[str, str, list[Extraction]]] = []
    for _, title, subtitle in vocab.GROUPS:
        if title in buckets:
            ordered.append((title, subtitle, buckets.pop(title)))
    for title, rows in buckets.items():
        ordered.append((title, "", rows))
    return ordered
