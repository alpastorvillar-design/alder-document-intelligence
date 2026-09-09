"""A minimal review view.

Deliberately small: server-rendered HTML plus a few fetch calls to the same
JSON API an automation would use. There is no separate front end, no build
step, and no second implementation of the review rules - the page can only do
what the API allows, which is the point.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Response
from fastapi.responses import RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.deps import db_session, require_api_key
from iep.db.models import Document, Dossier, Extraction, Finding
from iep.domain.enums import FieldStatus, FindingStatus
from iep.dossiers import service as dossiers
from iep.reporting.render import describe_locator, format_number

TEMPLATE_DIR = Path(__file__).parent.parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.filters["number"] = format_number

router = APIRouter(prefix="/ui", tags=["ui"], dependencies=[Depends(require_api_key)])


@router.get("/dossiers", response_class=Response, summary="The review queue (start here)")
def index(session: Session = Depends(db_session)) -> Response:
    """An HTML page listing the dossiers, newest first, with a link into each.

    Open `http://127.0.0.1:8000/ui/dossiers` in a browser. This is a minimal
    server-rendered screen so the evidence can be looked at without building
    a client; it is not a product front end.
    """
    rows = list(
        session.execute(select(Dossier).order_by(Dossier.created_at.desc()).limit(50)).scalars()
    )
    html = _env.get_template("index.html").render(dossiers=rows)
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("", include_in_schema=False)
def index_redirect() -> RedirectResponse:
    """`/ui` is a natural thing to type; send it to the list rather than 404."""
    return RedirectResponse(url="/ui/dossiers", status_code=307)


@router.get(
    "/dossiers/{dossier_id}",
    response_class=Response,
    summary="Review one dossier: findings, fields, evidence",
)
def review_view(dossier_id: uuid.UUID, session: Session = Depends(db_session)) -> Response:
    """The findings, then every extracted field with the place it came from.

    The buttons post to the `review` endpoints, so anything done here is
    recorded with an actor and a reason exactly as an API call would be.
    Approval is refused while a blocking finding is open.
    """
    dossier = dossiers.get(session, dossier_id)
    documents = {
        row.id: row.original_filename
        for row in session.execute(
            select(Document).where(Document.dossier_id == dossier_id)
        ).scalars()
    }
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
    html = _env.get_template("review.html").render(
        dossier=dossier,
        documents=documents,
        findings=findings,
        extractions=extractions,
        needs_review=[e for e in extractions if e.status == FieldStatus.NEEDS_REVIEW],
        open_findings=[f for f in findings if f.status == FindingStatus.OPEN],
        describe=describe_locator,
    )
    return Response(content=html, media_type="text/html; charset=utf-8")
