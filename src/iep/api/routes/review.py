"""Extractions, findings and the human decisions taken on them."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api.deps import db_session, require_api_key
from iep.db.models import Extraction as ExtractionRow
from iep.db.models import Finding as FindingRow
from iep.db.models import ReviewDecision as DecisionRow
from iep.domain.contracts import (
    DossierDecision,
    Extraction,
    FindingResolution,
    ReviewConfirmation,
    ReviewCorrection,
    ReviewDecision,
    ValidationFinding,
)
from iep.domain.enums import FieldStatus, FindingStatus, Severity
from iep.dossiers import service as dossiers
from iep.review import service as review

router = APIRouter(tags=["review"], dependencies=[Depends(require_api_key)])


@router.get(
    "/dossiers/{dossier_id}/extractions",
    response_model=list[Extraction],
    summary="Cada campo leído, y de dónde",
)
def list_extractions(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    field_status: FieldStatus | None = None,
    field_path: str | None = Query(default=None, max_length=200),
) -> list[Extraction]:
    """La capa de evidencia: una fila por valor, cada una con su `locator`.

    Un locator es una unión etiquetada, así que dice exactamente qué clase de
    sitio es: una página y un rango de caracteres, una caja de palabras de OCR
    con la confianza del motor, una hoja y una celda, una ruta JSON en la
    respuesta de una API, un selector CSS sobre una página capturada, o los
    identificadores de las extracciones de las que se derivó un total.

    `field_path` casa por prefijo — `invoice.` da todos los campos de factura.
    `status` es dónde está un valor dentro de la revisión, y
    `original_value_text` guarda lo que leyó la máquina cuando una persona no
    estuvo de acuerdo.
    """
    dossiers.get(session, dossier_id)
    stmt = (
        select(ExtractionRow)
        .where(ExtractionRow.dossier_id == dossier_id)
        .order_by(ExtractionRow.field_path.asc())
    )
    if field_status is not None:
        stmt = stmt.where(ExtractionRow.status == field_status)
    if field_path:
        stmt = stmt.where(ExtractionRow.field_path.startswith(field_path))
    return [Extraction.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get(
    "/dossiers/{dossier_id}/findings",
    response_model=list[ValidationFinding],
    summary="A qué han llegado las reglas",
)
def list_findings(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session),
    finding_status: FindingStatus | None = None,
    severity: Severity | None = None,
) -> list[ValidationFinding]:
    """Reglas deterministas y versionadas que cruzan cada fuente con las demás.

    `severity` es `BLOCKER`, `WARNING` o `INFO`; un `BLOCKER` abierto o
    aceptado impide aprobar. Cada incidencia nombra la regla, su versión, las
    extracciones y documentos a los que apunta, y un `detail` estructurado con
    las cifras que comparó.
    """
    dossiers.get(session, dossier_id)
    stmt = (
        select(FindingRow)
        .where(FindingRow.dossier_id == dossier_id)
        .order_by(FindingRow.rule_id.asc())
    )
    if finding_status is not None:
        stmt = stmt.where(FindingRow.status == finding_status)
    if severity is not None:
        stmt = stmt.where(FindingRow.severity == severity)
    return [ValidationFinding.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get(
    "/dossiers/{dossier_id}/decisions",
    response_model=list[ReviewDecision],
    summary="Qué decidió cada persona, y por qué",
)
def list_decisions(
    dossier_id: uuid.UUID, session: Session = Depends(db_session)
) -> list[ReviewDecision]:
    """Sólo añade, de la más antigua a la más nueva: corregir, confirmar,
    aceptar, descartar, aprobar, rechazar.

    Aquí nada se actualiza ni se borra, así que la secuencia es un registro y
    no un relato.
    """
    dossiers.get(session, dossier_id)
    stmt = (
        select(DecisionRow)
        .where(DecisionRow.dossier_id == dossier_id)
        .order_by(DecisionRow.created_at.asc())
    )
    return [ReviewDecision.model_validate(row) for row in session.execute(stmt).scalars()]


@router.post(
    "/extractions/{extraction_id}/correct",
    response_model=Extraction,
    summary="Esto se leyó mal; el valor es X",
)
def correct_extraction(
    extraction_id: uuid.UUID,
    payload: ReviewCorrection,
    session: Session = Depends(db_session),
) -> Extraction:
    """Registra el valor humano al lado del de la máquina, nunca encima.

    `original_value_text` conserva lo que se leyó, y con el cambio se guardan
    quién, cuándo y por qué. Pasa `expected_revision` y una edición sobre datos
    viejos falla con `409` en lugar de sobreescribir en silencio la decisión de
    otra persona; el valor corregido queda además protegido de que lo pise una
    nueva ejecución.
    """
    row = review.correct_field(
        session,
        extraction_id,
        actor=payload.actor,
        reason=payload.reason,
        new_value=payload.new_value,
        expected_revision=payload.expected_revision,
    )
    return Extraction.model_validate(row)


@router.post(
    "/extractions/{extraction_id}/confirm",
    response_model=Extraction,
    summary="He comprobado esto contra el documento",
)
def confirm_extraction(
    extraction_id: uuid.UUID,
    payload: ReviewConfirmation,
    session: Session = Depends(db_session),
) -> Extraction:
    """Cierra un campo que fue a revisión porque su confianza era baja.

    El valor no cambia; lo que cambia es que una persona con nombre se hace
    responsable de él. Mientras quede algún campo pendiente de revisión no se
    puede aprobar.
    """
    row = review.confirm_field(
        session,
        extraction_id,
        actor=payload.actor,
        reason=payload.reason,
        expected_revision=payload.expected_revision,
    )
    return Extraction.model_validate(row)


@router.post(
    "/findings/{finding_id}/resolve",
    response_model=ValidationFinding,
    summary="Aceptar o descartar una incidencia, con motivo",
)
def resolve_finding(
    finding_id: uuid.UUID,
    payload: FindingResolution,
    session: Session = Depends(db_session),
) -> ValidationFinding:
    """`accept: true` significa que la incidencia es real. No es una dispensa.

    Un bloqueante aceptado sigue impidiendo aprobar: lo que tiene que cambiar
    es la justificación, no el veredicto. `accept: false` la descarta como
    falso positivo y exige un motivo, que queda registrado: una comprobación
    que se puede saltar en silencio no es una comprobación.
    """
    row = review.resolve_finding(
        session,
        finding_id,
        actor=payload.actor,
        reason=payload.reason,
        accept=payload.accept,
    )
    return ValidationFinding.model_validate(row)


@router.post("/dossiers/{dossier_id}/approve", status_code=204, summary="Aprobar el expediente")
def approve_dossier(
    dossier_id: uuid.UUID,
    payload: DossierDecision,
    session: Session = Depends(db_session),
) -> None:
    """La única forma de aprobar un expediente. Ningún camino del pipeline
    llega a este estado.

    Se rechaza con `409` mientras quede algún campo pendiente de revisión o
    algún bloqueante abierto o aceptado. Un expediente aprobado es inmutable:
    corregirlo significa crear uno nuevo, de modo que la auditoría siga siendo
    el registro de lo que realmente se entregó.
    """
    review.approve(session, dossier_id, actor=payload.actor, reason=payload.reason)


@router.post("/dossiers/{dossier_id}/reject", status_code=204, summary="Rechazar el expediente")
def reject_dossier(
    dossier_id: uuid.UUID,
    payload: DossierDecision,
    session: Session = Depends(db_session),
) -> None:
    """Devuelve la justificación, con un motivo, a nombre de quien la rechaza.

    A diferencia de aprobar, esto no es terminal: un expediente rechazado
    puede recibir documentos nuevos y volver a procesarse.
    """
    review.reject(session, dossier_id, actor=payload.actor, reason=payload.reason)
