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
from iep.api.deps import db_session, object_store, require_api_key, settings_dep
from iep.api.errors import NotFoundError
from iep.config import Settings
from iep.db.models import Document, Dossier, Extraction, Finding, ProcessingJob
from iep.domain.enums import DocumentStatus, FieldStatus, FindingStatus, MediaKind, Severity
from iep.dossiers import service as dossiers
from iep.ingestion.service import NOT_STORED
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


def original_opens_inline(media_kind: object) -> bool:
    """Si un navegador mostrará este tipo o lo entregará como fichero.

    Se lee de la misma tabla que usa la cabecera de la respuesta, para que el
    botón no pueda prometer «abrir» mientras la cabecera dice `attachment`. Un
    libro de Excel es el único tipo que ningún navegador pinta, y una etiqueta
    que lo ignoraba dejaba a quien revisa preguntándose si la descarga era un
    fallo.
    """
    try:
        _, disposition = ORIGINAL_MEDIA[MediaKind(str(media_kind))]
    except (KeyError, ValueError):
        return False
    return disposition == "inline"


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
    original_opens_inline=original_opens_inline,
    rule=vocab.rule,
    field_label=vocab.field_label,
    field_label_short=vocab.field_label_short,
    locator_summary=vocab.locator_summary,
    detail_rows=vocab.detail_rows,
    evidence_links=vocab.evidence_links,
    unit_of=vocab.unit_of,
)

router = APIRouter(prefix="/ui", tags=["ui"], dependencies=[Depends(require_api_key)])


def _page(name: str, **context: Any) -> Response:
    html = _env.get_template(name).render(**context)
    return Response(content=html, media_type="text/html; charset=utf-8")


def default_call_page_url(settings: Settings) -> str:
    """The call page the intake form suggests before anybody types one.

    The local source simulator serves the personnel registry and the published
    call page, so the registry address this process is configured with is one
    its worker is known to reach: `devsources` inside Compose, 127.0.0.1 when
    the API and the worker run on the host. The form used to carry the Compose
    address written into the template, and on the host the scraper refused it:
    a dossier submitted as the form arrived got an EXTERNAL_SOURCE_UNAVAILABLE
    blocker and could never be approved.
    """
    return f"{settings.registry_api_base_url.rstrip('/')}/public/convocatoria.html"


# --------------------------------------------------------------------------
# 1. The queue
# --------------------------------------------------------------------------


@router.get("", include_in_schema=False)
def index_redirect() -> RedirectResponse:
    """`/ui` es lo natural de teclear: llévalo a la bandeja en vez de dar 404."""
    return RedirectResponse(url="/ui/dossiers", status_code=307)


@router.get("/dossiers", response_class=Response, summary="La bandeja de revisión (empieza aquí)")
def queue(session: Session = Depends(db_session, scope="function")) -> Response:
    """Los expedientes que esperan una decisión, del más nuevo al más viejo,
    con lo que bloquea a cada uno.

    Abre `http://127.0.0.1:8000/ui/dossiers` en un navegador. Esta es la
    pantalla desde la que trabaja quien revisa; la API JSON que hay debajo es
    lo que usa una automatización.
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
    summary="Abrir un expediente y soltarle sus documentos",
)
def new_dossier(settings: Settings = Depends(settings_dep)) -> Response:
    """La pantalla de alta: primero los datos de la justificación, luego los
    ficheros que la soportan.

    La página usa `POST /dossiers`, después un `POST /dossiers/{id}/documents`
    por fichero, y después `POST /dossiers/{id}/process` — las mismas tres
    llamadas que haría una integración, que es la razón de que un rechazo aquí
    se vea exactamente igual que un rechazo allí.

    El formulario llega relleno con un expediente de ejemplo, y la página de
    convocatoria apunta al simulador local por la dirección que usa este mismo
    despliegue: `devsources` dentro de Compose y `127.0.0.1` cuando la API y el
    worker corren en el host.
    """
    return _page("new.html", call_page_url=default_call_page_url(settings))


# --------------------------------------------------------------------------
# 3. Progress
# --------------------------------------------------------------------------


@router.get(
    "/dossiers/{dossier_id}/progress",
    response_class=Response,
    summary="Ver la ejecución mientras el worker trabaja",
)
def progress(
    dossier_id: uuid.UUID, session: Session = Depends(db_session, scope="function")
) -> Response:
    """Consulta el trabajo hasta que termina y entonces va a la revisión.

    Las etapas que se muestran son las del propio pipeline: capturar fuentes
    externas, leer documentos, clasificar, agregar, validar.
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
    summary="Revisar un expediente: incidencias, campos, evidencia",
)
def review_view(
    dossier_id: uuid.UUID, session: Session = Depends(db_session, scope="function")
) -> Response:
    """A qué han llegado las reglas, y luego cada campo con un camino de vuelta
    a su origen.

    Los botones llaman a los endpoints de `review`, así que todo lo que se
    haga aquí queda registrado con quién y por qué, igual que si viniera de la
    API. Aprobar se rechaza mientras haya una incidencia bloqueante abierta.
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
        # Keyed by string: a finding stores its document ids as strings in
        # JSONB, so a UUID-keyed map missed every lookup and the template fell
        # back to printing the raw identifier.
        document_names={str(row.id): row.original_filename for row in documents},
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
        # Nested rather than flat: the invoice, timesheet and registry groups
        # hold the same fields once per document, row or person, and read as a
        # puzzle without a heading saying which is which.
        grouped=vocab.group_sections(
            extractions, {str(row.id): row.original_filename for row in documents}
        ),
        # A finding names the extractions and documents it points at. Resolving
        # them here is what lets the card offer a way straight to the evidence
        # instead of describing it.
        extraction_by_id={str(row.id): row for row in extractions},
        can_approve=not blockers and not needs_review,
        copilot_prompts=vocab.copilot_prompts(finding.rule_id for finding in findings),
    )


# --------------------------------------------------------------------------
# 5. Evidence
# --------------------------------------------------------------------------


@router.get(
    "/evidence/{extraction_id}",
    response_class=Response,
    summary="Ver de dónde sale un valor, con el sitio marcado",
)
def evidence(
    extraction_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    store: ObjectStore = Depends(object_store),
) -> Response:
    """El documento del que se leyó este valor, con un recuadro sobre la
    lectura.

    Es el locator recorrido hacia atrás. En un escaneo es la imagen con las
    palabras que leyó el motor recuadradas; en un PDF, la página rasterizada
    con el texto citado recuadrado; en un libro de Excel, la celda con sus
    vecinas; en una página capturada, el fragmento que casó y el selector.
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
        # On this screen the reviewer is already asking "where else does this
        # appear", so the first suggestion is about the field in front of them.
        copilot_prompts=vocab.copilot_prompts(
            (
                row
                for row in session.execute(
                    select(Finding.rule_id).where(Finding.dossier_id == extraction.dossier_id)
                ).scalars()
            ),
            field_label_for=vocab.field_label(extraction.field_path),
        ),
    )


@router.get(
    "/evidence/{extraction_id}/image",
    response_class=Response,
    include_in_schema=False,
)
def evidence_image(
    extraction_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    store: ObjectStore = Depends(object_store),
) -> Response:
    """La página que hay detrás de una vista de evidencia, como imagen sobre la
    que el navegador puede dibujar.
    """
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


@router.get(
    "/documents/{document_id}/original",
    response_class=Response,
    summary="El documento tal como se entregó",
)
def original_document(
    document_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    store: ObjectStore = Depends(object_store),
) -> Response:
    """Los bytes almacenados, sin tocar, con su propio tipo de medio.

    El visor de evidencia dibuja sobre una copia rasterizada para poder poner
    un recuadro en la página. Esa copia no es el documento: abrirla para un
    PDF le daba a quien revisa un PNG de la primera página, y para un libro de
    Excel no había nada que abrir. Esto sirve lo que se entregó, así que un PDF
    se abre en el visor de PDF, un escaneo se abre como imagen y un libro se
    descarga y se abre en Excel.

    Un documento rechazado no tiene bytes —por diseño, nada ilegible se
    almacena—, así que responde 404 en lugar de un fichero vacío.
    """
    document = session.get(Document, document_id)
    if document is None:
        raise NotFoundError("No hay ningun documento con ese identificador.")
    data = _document_bytes(store, document)
    if data is None:
        raise NotFoundError(
            "Este documento no tiene contenido almacenado: se rechazo en la entrada."
        )

    media_type, disposition = ORIGINAL_MEDIA.get(
        MediaKind(str(document.media_kind)), ("application/octet-stream", "attachment")
    )
    filename = _ascii_filename(document.original_filename)
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            # Addressed by content digest, so these bytes never change.
            "Cache-Control": "private, max-age=3600",
        },
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _document_bytes(store: ObjectStore, document: Document | None) -> bytes | None:
    """The stored bytes, or `None` when there are none to serve.

    `None` is an expected answer, not a failure: a refused submission is
    recorded and never stored. Callers turn it into a 404 with a sentence, or
    into an evidence view with no page to draw on.
    """
    if document is None or not document.storage_key:
        return None
    if document.storage_key == NOT_STORED:
        # The sentinel a refused upload is recorded with. It is deliberately
        # not a key, so asking the store for it raises `ValueError` rather than
        # "not found" - and that is how three routes came to answer 500 where
        # this file promises 404.
        return None
    try:
        # `ValueError` included: any key the store cannot parse means there are
        # no bytes to serve, which is this function's answer, not a crash.
        return store.get(document.storage_key)
    except (ObjectNotFoundError, OSError, ValueError):
        return None


def _is_jpeg(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff"


# What the original bytes should be served as, and whether a browser can show
# them itself. A workbook has no viewer, so it is sent as a download and the
# operating system opens Excel; a PDF and an image render in place.
ORIGINAL_MEDIA = {
    MediaKind.PDF: ("application/pdf", "inline"),
    MediaKind.PNG: ("image/png", "inline"),
    MediaKind.JPEG: ("image/jpeg", "inline"),
    MediaKind.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "attachment",
    ),
    MediaKind.JSON: ("application/json; charset=utf-8", "inline"),
    MediaKind.HTML: ("text/plain; charset=utf-8", "inline"),
}


def _ascii_filename(name: str) -> str:
    """A filename a `Content-Disposition` header can carry safely.

    The header is latin-1 on the wire, and the stored name came from an
    upload, so it is neither trusted nor assumed to encode. Quotes and control
    characters are dropped rather than escaped.
    """
    cleaned = "".join(c for c in name if c.isprintable() and c not in '"\\')
    return cleaned.encode("ascii", "ignore").decode("ascii") or "documento"


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
