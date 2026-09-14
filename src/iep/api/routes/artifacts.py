"""Reports, exports, audit history and evidence lookup."""

from __future__ import annotations

import unicodedata
import urllib.parse
import uuid
from typing import Literal, cast

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.api import vocabulary as vocab
from iep.api.deps import db_session, require_api_key, settings_dep
from iep.api.errors import (
    NotFoundError,
    ServiceUnavailableError,
    UnprocessableDocumentError,
)
from iep.audit import service as audit
from iep.config import Settings
from iep.db.models import Report as ReportRow
from iep.domain.contracts import (
    AuditEvent,
    DossierReport,
    ModelOption,
    RagAnswer,
    RagCitation,
    RagProviderName,
    RagQuestion,
    RagStatus,
    SearchModeName,
    UsageReport,
)
from iep.domain.enums import AuditAction
from iep.dossiers import service as dossiers
from iep.reporting import pdf as report_pdf
from iep.reporting import render
from iep.retrieval import budget as rag_budget
from iep.retrieval import catalogue
from iep.retrieval import search as retrieval
from iep.retrieval import usage as rag_usage
from iep.retrieval.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingProviderError,
    build_embedding_provider,
)
from iep.retrieval.prompting import AllEvidenceWithheldError
from iep.retrieval.rag import RAG_PROMPT_VERSION, RagProviderError, build_rag_generator
from iep.retrieval.rag_cli import available_tools

router = APIRouter(tags=["artifacts"], dependencies=[Depends(require_api_key)])


@router.post(
    "/dossiers/{dossier_id}/reports",
    response_model=DossierReport,
    status_code=201,
    summary="Generar el informe para una persona",
)
def generate_report(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> DossierReport:
    """Genera un informe HTML autocontenido y registra la huella de su contenido.

    El informe es una instantánea: guarda el estado del expediente y las
    cifras con las que se generó, de modo que un informe antiguo sigue
    diciendo lo que era cierto cuando se emitió. Se lee luego en
    `reports/latest.html`.
    """
    dossiers.get(session, dossier_id)
    rendered = render.render_html(session, dossier_id)
    row = render.persist(session, dossier_id, rendered, report_root=settings.report_root)
    session.flush()
    return DossierReport.model_validate(row)


@router.get(
    "/dossiers/{dossier_id}/reports",
    response_model=list[DossierReport],
    summary="Informes generados hasta ahora",
)
def list_reports(
    dossier_id: uuid.UUID, session: Session = Depends(db_session, scope="function")
) -> list[DossierReport]:
    """Del más reciente al más antiguo, cada uno con su huella y el estado
    con el que se generó.
    """
    dossiers.get(session, dossier_id)
    stmt = (
        select(ReportRow)
        .where(ReportRow.dossier_id == dossier_id)
        .order_by(ReportRow.generated_at.desc())
    )
    return [DossierReport.model_validate(row) for row in session.execute(stmt).scalars()]


@router.get(
    "/dossiers/{dossier_id}/reports/latest.html",
    response_class=Response,
    summary="El informe más reciente",
)
def latest_report(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """El informe en sí, en HTML. Ábrelo en un navegador.

    Responde `404` hasta que se haya generado alguno — ver `POST .../reports`.
    """
    dossiers.get(session, dossier_id)
    _, html = _stored_report(session, dossier_id, settings)
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get(
    "/dossiers/{dossier_id}/reports/latest.pdf",
    response_class=Response,
    summary="El informe más reciente en PDF, generado aquí",
)
def latest_report_pdf(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> Response:
    """El mismo informe, renderizado a PDF en esta máquina.

    No es el diálogo de imprimir del navegador. Un PDF hecho por esa vía llegó
    como 26 mapas de bits, sin fuentes incrustadas y sin texto seleccionable
    —«imprimir como imagen»—, que deja un documento archivado sin poder
    buscarse y con las letras sucias. Esto renderiza el HTML almacenado con el
    motor para el que está escrita su hoja de estilos.

    Lo que lleva huella es el HTML: el PDF es una representación suya, y
    `X-Report-Sha256` dice de qué informe salió este fichero. Chromium estampa
    una fecha de creación, así que dos renderizados del mismo informe no son
    idénticos byte a byte — y por eso la huella nombra el origen y no la
    salida.
    """
    dossier = dossiers.get(session, dossier_id)
    row, html = _stored_report(session, dossier_id, settings)
    try:
        content = report_pdf.render(html, binary=settings.pdf_renderer or None)
    except report_pdf.PdfUnavailableError as exc:
        raise ServiceUnavailableError(str(exc)) from exc
    except report_pdf.PdfRenderError as exc:
        raise ServiceUnavailableError(f"No se pudo generar el PDF: {exc}") from exc
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": _disposition(
                f"Informe de justificación {dossier.reference}.pdf"
            ),
            "X-Report-Sha256": row.content_sha256,
        },
    )


def _disposition(filename: str) -> str:
    """`Content-Disposition` for a filename a person would recognise.

    It used to be `informe-<uuid>-<digest>.pdf`: unambiguous, and unreadable
    in a folder of them. The name now says what the document is and which
    dossier it belongs to, which is how it will be filed. The digest it used
    to carry is not lost - `X-Report-Sha256` names the report this rendering
    came from, and the audit trail holds it - but two versions of one
    dossier's report do now arrive under the same name, and the browser
    resolves that by appending a number.

    Two parameters, because the name has an accent in it. `filename` carries
    an ASCII-folded fallback for anything that cannot read the other, and
    `filename*` carries the real one, percent-encoded as RFC 5987 requires.
    Sending only the UTF-8 bytes in `filename` is what produces
    `Informe de justificaciÃ³n` in a download folder.
    """
    ascii_name = unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode("ascii")
    # The two apostrophes are part of the grammar, not quoting: the parameter
    # is `charset'language'value` and the language is deliberately empty.
    encoded = "UTF-8''" + urllib.parse.quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*={encoded}'


def _stored_report(
    session: Session, dossier_id: uuid.UUID, settings: Settings
) -> tuple[ReportRow, bytes]:
    """The newest report row and its bytes, or a 404 naming which is missing."""
    row = session.execute(
        select(ReportRow)
        .where(ReportRow.dossier_id == dossier_id)
        .order_by(ReportRow.generated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("No report has been generated for this dossier yet.")
    path = settings.report_root / row.storage_key
    if not path.exists():
        # Naming the directory matters more than it looks. Two API processes
        # can share this database and have different `IEP_REPORT_ROOT` values -
        # a container with a volume, and a developer's process with a local
        # folder - and then a row written by one names a file only that one can
        # read. Without the path in the message the symptom is an unexplained
        # 404 on a report that visibly exists.
        raise NotFoundError(
            "El informe está registrado pero su fichero no está donde este "
            "proceso guarda los informes. Si lo generó otro proceso con otro "
            "IEP_REPORT_ROOT, vuelve a generarlo aquí.",
            {
                "report_id": str(row.id),
                "expected_file": row.storage_key,
                "report_root": str(settings.report_root),
            },
        )
    return row, path.read_bytes()


@router.get(
    "/dossiers/{dossier_id}/export.json",
    response_class=Response,
    summary="Todo, en JSON, para otro sistema",
)
def export_json(
    dossier_id: uuid.UUID, session: Session = Depends(db_session, scope="function")
) -> Response:
    """El expediente, sus documentos, cada extracción con su locator y cada
    incidencia.

    Es el equivalente legible por máquina del informe: un sistema de aguas
    abajo se lleva la evidencia, no sólo los totales.
    """
    dossiers.get(session, dossier_id)
    return Response(
        content=render.export_json(session, dossier_id),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{dossier_id}.json"'},
    )


@router.get(
    "/dossiers/{dossier_id}/export.csv",
    response_class=Response,
    summary="Todo, en CSV, para una hoja de cálculo",
)
def export_csv(
    dossier_id: uuid.UUID,
    dialect: Literal["rfc4180", "excel"] = Query(
        default="rfc4180",
        description=(
            "`rfc4180` is commas and UTF-8 with no byte order mark, which is what "
            "pandas, R, DuckDB and `csv.reader` expect. `excel` is semicolons, CRLF "
            "and a BOM, which is what a double-click needs on an install whose list "
            "separator is a semicolon - otherwise every row arrives in column A."
        ),
    ),
    session: Session = Depends(db_session, scope="function"),
) -> Response:
    """Una fila por extracción, con el locator escrito de forma legible.

    Las celdas que empiezan por `=`, `+`, `-` o `@` se neutralizan antes de
    escribirse: un valor leído de un documento en el que no se confía no puede
    convertirse en una fórmula cuando alguien abra el fichero en Excel.
    """
    dossiers.get(session, dossier_id)
    suffix = "-excel" if dialect == "excel" else ""
    return Response(
        content=render.export_csv(session, dossier_id, dialect=dialect),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{dossier_id}{suffix}.csv"'},
    )


@router.get(
    "/dossiers/{dossier_id}/audit",
    response_model=list[AuditEvent],
    summary="Todo lo que le ha pasado a este expediente",
)
def dossier_audit(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> list[AuditEvent]:
    """Sólo añade: altas, rechazos, trabajos, transiciones, decisiones humanas.

    Cada evento se escribe dentro de la transacción del cambio que describe,
    así que un rollback no puede dejar un registro afirmando que algo pasó.
    Cada uno lleva el `correlation_id` de la petición que lo causó.
    """
    dossiers.get(session, dossier_id)
    return [
        AuditEvent.model_validate(row) for row in audit.history(session, dossier_id, limit=limit)
    ]


@router.get(
    "/dossiers/{dossier_id}/evidence",
    summary="Buscar una frase en los documentos de este expediente",
)
def find_evidence(
    dossier_id: uuid.UUID,
    q: str = Query(min_length=2, max_length=200),
    limit: int = Query(default=5, ge=1, le=50),
    session: Session = Depends(db_session, scope="function"),
    settings: Settings = Depends(settings_dep),
    mode: retrieval.SearchMode = Query(default="lexical"),
) -> dict[str, object]:
    """Busca sólo en este expediente y devuelve los locators originales.

    `lexical` usa la búsqueda de texto completo de PostgreSQL en español.
    `vector` usa distancia coseno exacta de pgvector. `hybrid` fusiona las dos
    posiciones con RRF. Este endpoint es recuperación, no generación; el de
    preguntas, aparte, es el punto de integración opcional con un modelo.
    """
    dossiers.get(session, dossier_id)
    provider = None
    if mode == "lexical":
        hits = retrieval.search(session, dossier_id, q, limit=limit)
        method = "postgresql Spanish full-text search"
    else:
        try:
            provider = build_embedding_provider(settings)
            if provider is None:
                raise ServiceUnavailableError(
                    "Vector retrieval is disabled. Configure an embedding provider first."
                )
            query_vector = provider.embed([q]).vectors[0]
            if mode == "vector":
                hits = retrieval.vector_search(
                    session,
                    dossier_id,
                    query_vector,
                    embedding_config_hash=provider.config_hash(),
                    limit=limit,
                )
                method = "exact pgvector cosine search"
            else:
                hits = retrieval.hybrid_search(
                    session,
                    dossier_id,
                    q,
                    query_vector,
                    embedding_config_hash=provider.config_hash(),
                    limit=limit,
                )
                method = "reciprocal-rank fusion of lexical and pgvector search"
        except EmbeddingProviderError as exc:
            raise ServiceUnavailableError("Vector retrieval is unavailable.") from exc
    return {
        "query": q,
        "mode": mode,
        "method": method,
        "embedding": (
            {
                "provider": provider.name,
                "model": provider.model,
                "learned_model": provider.name != "hashing",
            }
            if provider is not None
            else None
        ),
        "results": [
            {
                "document": hit.document_name,
                "document_id": str(hit.document_id),
                "ordinal": hit.ordinal,
                "rank": round(hit.rank, 6),
                "lexical_rank": (
                    round(hit.lexical_rank, 6) if hit.lexical_rank is not None else None
                ),
                "vector_similarity": (
                    round(hit.vector_similarity, 6) if hit.vector_similarity is not None else None
                ),
                "text": hit.text,
                "locator": hit.locator,
                "where": render.describe_locator(hit.locator),
            }
            for hit in hits
        ],
    }


@router.post(
    "/dossiers/{dossier_id}/questions",
    response_model=RagAnswer,
    summary="Redactar una respuesta citando la evidencia del expediente",
)
def answer_evidence_question(
    dossier_id: uuid.UUID,
    request: RagQuestion,
    session: Session = Depends(db_session, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> RagAnswer:
    """Integración opcional con un modelo; apagada por defecto, y nunca cambia
    el estado del expediente.

    Del proceso sólo salen los k fragmentos mejor posicionados. El generador
    no recibe herramientas, cada cita que devuelve se comprueba contra esos
    fragmentos, y el texto de los documentos se trata como dato en el que no
    se confía. La respuesta de un modelo es un borrador, nunca una decisión.
    """
    dossiers.get(session, dossier_id)
    # Checked before any work: refusing after the model has already answered
    # would spend the call it was meant to prevent.
    try:
        allowance = rag_budget.guard(session, settings)
    except rag_budget.BudgetExhaustedError as exc:
        raise ServiceUnavailableError(str(exc)) from exc

    # A model name from a client reaches `argv` on the CLI backends, so it is
    # resolved against the catalogue of what this process can actually launch
    # rather than trusted. An unknown id is refused, not quietly defaulted:
    # answering with a different model than the one asked for is a lie.
    chosen = catalogue.resolve(settings, request.model)
    if request.model and chosen is None:
        raise UnprocessableDocumentError(
            f"El modelo «{request.model}» no está disponible en esta máquina.",
            {"available": [option.id for option in catalogue.catalogue(settings)]},
        )

    # Built before any retrieval: "the copilot is off" is a different answer
    # from "the search found nothing", and checking availability last meant a
    # reviewer with generation disabled was told the second one.
    try:
        generator = build_rag_generator(settings, chosen)
    except RagProviderError as exc:
        raise ServiceUnavailableError(
            f"El proveedor opcional de respuestas no está disponible: {exc}"
        ) from exc

    try:
        if request.retrieval_mode == "lexical":
            hits = retrieval.search(session, dossier_id, request.question, limit=request.top_k)
        else:
            embedding_provider = build_embedding_provider(settings)
            if embedding_provider is None:
                raise ServiceUnavailableError(
                    "Vector retrieval is disabled. Configure an embedding provider first."
                )
            query_vector = embedding_provider.embed([request.question]).vectors[0]
            if request.retrieval_mode == "vector":
                hits = retrieval.vector_search(
                    session,
                    dossier_id,
                    query_vector,
                    embedding_config_hash=embedding_provider.config_hash(),
                    limit=request.top_k,
                    min_similarity=settings.effective_min_similarity,
                )
            else:
                hits = retrieval.hybrid_search(
                    session,
                    dossier_id,
                    request.question,
                    query_vector,
                    embedding_config_hash=embedding_provider.config_hash(),
                    limit=request.top_k,
                    min_similarity=settings.effective_min_similarity,
                )
        if not hits:
            # Two situations were being reported as one, and the message told a
            # reviewer to reprocess a dossier that was perfectly fine.
            #
            # Nothing *indexed* is a configuration problem: reprocessing is the
            # fix, and saying so is useful. Nothing *matched* is not a problem
            # at all - it is the answer, and for lexical retrieval it is the
            # most trustworthy answer this system gives, because "those words
            # do not appear in this expediente" is a fact about the documents.
            # Returning it without calling a model is also the only honest
            # thing to do: there is nothing to ground an answer in, and a call
            # would spend budget on an empty prompt.
            if not retrieval.has_indexed_evidence(session, dossier_id):
                raise ServiceUnavailableError(
                    "Este expediente no tiene evidencia indexada. Vuelve a procesarlo."
                )
            mismatch = (
                ""
                if request.retrieval_mode == "lexical"
                else _configuration_mismatch(session, dossier_id, settings)
            )
            return _nothing_to_ground(
                request, mismatch or _why_nothing_matched(request.retrieval_mode)
            )
        # Evidence search may return a document flagged as carrying
        # instructions aimed at an automated reader - a reviewer has to be able
        # to find it. Quoting it into a prompt is a different act, so it is
        # dropped here, between retrieval and generation.
        hits, withheld = retrieval.without_hostile_documents(session, dossier_id, hits)
        if not hits:
            # Not a failure and not something a retry fixes: every segment the
            # search found belongs to a document the rules flagged. Saying so
            # is the answer, and no model is called.
            return _nothing_to_ground(
                request,
                (
                    f"Los {withheld} fragmento(s) que la búsqueda encontró están todos en "
                    f"documentos marcados por llevar instrucciones dirigidas a un lector "
                    f"automático, así que no se ha enviado ninguno a un modelo. Revísalos "
                    f"a mano: son exactamente el tipo de documento que hay que mirar."
                ),
                withheld_hostile=withheld,
            )
        generation = generator.generate(request.question, hits)
    except AllEvidenceWithheldError as exc:
        # The screen removed everything, so no model was called.
        return _nothing_to_ground(
            request,
            (
                f"Los {exc.withheld} fragmento(s) que la búsqueda encontró contienen "
                f"órdenes dirigidas a un sistema automático, así que no se ha enviado "
                f"ninguno a un modelo y no hay respuesta que dar. Míralos tú: es el "
                f"tipo de documento que conviene revisar a mano."
            ),
            withheld_directive=exc.withheld,
        )
    except (EmbeddingProviderError, RagProviderError) as exc:
        # The provider's own reason travels with the refusal. Swallowing it
        # left a reviewer with "unavailable" and no way to tell a missing key
        # from a timeout from a CLI that is not on this process's PATH - and
        # these messages are about configuration, not about a dossier.
        raise ServiceUnavailableError(
            f"El proveedor opcional de respuestas no está disponible: {exc}"
        ) from exc

    by_evidence_id = {f"E{position}": hit for position, hit in enumerate(hits, start=1)}
    citations = []
    for evidence_id in generation.citation_ids:
        hit = by_evidence_id[evidence_id]
        citations.append(
            RagCitation(
                evidence_id=evidence_id,
                document=hit.document_name,
                document_id=hit.document_id,
                ordinal=hit.ordinal,
                text=hit.text,
                locator=hit.locator,
                # `vocabulary`, not `render.describe_locator`: the citation is
                # shown on a Spanish screen, and "page 1, characters 874-1772"
                # was the last English string left in the answer panel.
                where=vocab.locator_summary(hit.locator),
            )
        )
    # A read-only answer still leaves a trace: which question, which provider
    # and model, how much evidence it saw, and whether it claimed the evidence
    # was enough. Without this the one place a language model touched the
    # dossier is the only place with no record - and the call budget, which
    # counts these events, would have nothing to count.
    audit.record(
        session,
        action=AuditAction.EVIDENCE_QUESTION_ANSWERED,
        dossier_id=dossier_id,
        payload={
            "question": request.question,
            "retrieval_mode": request.retrieval_mode,
            "provider": generation.provider,
            "model": generation.model,
            "segments_retrieved": len(hits),
            "segments_withheld": withheld,
            "segments_withheld_directive": generation.withheld_directives,
            "citations": list(generation.citation_ids),
            "sufficient_evidence": generation.sufficient_evidence,
            "prompt_version": generation.prompt_version,
            "prompt_sha256": generation.prompt_sha256,
            "input_tokens": generation.input_tokens,
            "output_tokens": generation.output_tokens,
            "budget_used_before": allowance.used,
            "budget_ceiling": allowance.ceiling,
        },
    )
    session.commit()

    return RagAnswer(
        question=request.question,
        answer=generation.answer,
        sufficient_evidence=generation.sufficient_evidence,
        citations=citations,
        withheld_hostile_segments=withheld,
        withheld_directive_segments=generation.withheld_directives,
        retrieval_mode=request.retrieval_mode,
        generation_provider=generation.provider,
        generation_model=generation.model,
        input_tokens=generation.input_tokens,
        output_tokens=generation.output_tokens,
        prompt_version=generation.prompt_version,
        prompt_sha256=generation.prompt_sha256,
    )


@router.get(
    "/dossiers/{dossier_id}/questions",
    response_model=RagStatus,
    summary="Si se puede producir una respuesta con evidencia, y en qué condiciones",
)
def rag_status(
    dossier_id: uuid.UUID,
    session: Session = Depends(db_session, scope="function"),
    settings: Settings = Depends(settings_dep),
) -> RagStatus:
    """El estado de la integración opcional con un modelo, para pintarlo en una
    pantalla.

    Se puede consultar a propósito incluso con la generación apagada:
    «apagada, y este es el interruptor» es la respuesta más útil, y es la que
    necesita quien tiene delante un cuadro en gris.
    """
    dossiers.get(session, dossier_id)
    allowance = rag_budget.current(session, settings)
    provider = settings.rag_provider
    options = catalogue.catalogue(settings)

    modes: list[str] = ["lexical"]
    if settings.embedding_provider != "disabled":
        modes += ["vector", "hybrid"]

    reason = ""
    cli_tool: str | None = None
    cli_available = False
    model: str | None = None
    available = available_tools()
    # Reported here as well as in the empty answer: a reviewer should see that
    # retrieval cannot work before spending a question finding out.
    retrieval_warning = _configuration_mismatch(session, dossier_id, settings)

    if provider == "disabled":
        reason = (
            "La generación está desactivada. Se enciende con "
            "IEP_RAG_PROVIDER=ollama (un modelo local, sin clave y sin salir "
            "de esta máquina), IEP_RAG_PROVIDER=cli (el CLI de claude o codex) "
            "o IEP_RAG_PROVIDER=openai con clave e IEP_ALLOW_EXTERNAL_AI=true."
        )
    elif provider == "ollama":
        model = settings.ollama_model or "sin elegir"
        if not options:
            reason = (
                f"No se ve ningún modelo en Ollama ({settings.ollama_base_url}). "
                f"Arráncalo y descarga alguno con `ollama pull`."
            )
        elif not settings.ollama_model:
            reason = (
                "Falta IEP_OLLAMA_MODEL, o elige un modelo en el desplegable antes de preguntar."
            )
    elif provider == "cli":
        cli_tool = settings.rag_cli_tool
        cli_available = available.get(cli_tool, False)
        model = settings.rag_cli_model or f"por defecto de {cli_tool}"
        if not cli_available:
            reason = (
                f"El CLI «{cli_tool}» no está en el PATH de este proceso. La API se "
                f"ejecuta en un contenedor por defecto y el CLI está instalado en el "
                f"host, así que hay que arrancar la API en el host para usarlo."
            )
    else:
        model = settings.openai_rag_model
        if not settings.allow_external_ai:
            reason = (
                "Falta IEP_ALLOW_EXTERNAL_AI=true, que es el consentimiento "
                "explícito de salida de datos."
            )
        elif not settings.openai_api_key:
            reason = "Falta la clave de API del proveedor alojado."

    if not reason and allowance.exhausted:
        reason = (
            f"El presupuesto local de consultas está agotado: {allowance.used} de "
            f"{allowance.ceiling} en los últimos {allowance.window_days} días, y se "
            f"detiene al llegar a {allowance.stop_at}."
        )

    return RagStatus(
        enabled=not reason,
        # `cast`, not `type: ignore`: the settings validator and the contract's
        # Literal list the same four values, and a silenced error here is how
        # "ollama" reached a Literal that did not have it.
        provider=cast(RagProviderName, provider),
        cli_tool=cli_tool,
        cli_available=cli_available,
        available_cli_tools=sorted(name for name, present in available.items() if present),
        model=model,
        retrieval_modes=cast(list[SearchModeName], modes),
        embedding_provider=settings.embedding_provider,
        embedding_is_learned=settings.embedding_provider != "hashing",
        min_similarity=settings.effective_min_similarity,
        unavailable_reason=reason,
        retrieval_warning=retrieval_warning,
        budget_used=allowance.used,
        budget_ceiling=allowance.ceiling,
        budget_stop_at=allowance.stop_at,
        budget_window_days=allowance.window_days,
        budget_exhausted=allowance.exhausted,
        models=[
            ModelOption(
                id=option.id,
                backend=option.backend,
                label=option.label,
                local=option.local,
                note=option.note,
            )
            for option in options
        ],
        usage=_usage_report(session, settings),
    )


def _configuration_mismatch(session: Session, dossier_id: uuid.UUID, settings: Settings) -> str:
    """Whether this process can compare vectors with the ones stored, in words.

    Returns the empty string when it can, which is the common case. When it
    cannot, the sentence names both models and the command that fixes it,
    because the alternative - an empty result - is indistinguishable from a
    question the expediente genuinely does not answer.
    """
    if settings.embedding_provider == "disabled":
        return ""
    stored = retrieval.stored_embeddings(session, dossier_id)
    if not stored:
        return ""
    try:
        provider = build_embedding_provider(settings)
    except EmbeddingConfigurationError as exc:
        return str(exc)
    if provider is None:
        return ""
    try:
        current = provider.config_hash()
    except (EmbeddingConfigurationError, EmbeddingProviderError) as exc:
        # The width of an Ollama model is discovered by asking it, so this is
        # also how "Ollama is not running" reaches the screen.
        return str(exc)
    if any(item.config_hash == current for item in stored):
        return ""
    indexed = ", ".join(
        f"{item.provider}/{item.model} ({item.chunks} fragmentos)" for item in stored
    )
    return (
        f"La búsqueda vectorial no puede usar lo que hay indexado: los fragmentos de "
        f"este expediente se crearon con {indexed}, y este proceso está configurado "
        f"con {provider.name}/{provider.model}. Vuelve a indexar con "
        f"`iep reindex --reference <referencia>` o configura el modelo anterior. "
        f"Mientras no coincidan, los modos vectorial e híbrido no encontrarán nada."
    )


def _why_nothing_matched(mode: str) -> str:
    """Why an empty result happened, in terms of the mode that produced it.

    One sentence explained lexical retrieval whichever mode had run, so
    somebody in vector mode was told about a word search they had not asked
    for.
    """
    shared = (
        "La búsqueda no ha encontrado ningún fragmento para esa pregunta, así que "
        "no se ha consultado a ningún modelo. "
    )
    if mode == "lexical":
        return shared + (
            "En modo léxico eso significa que ninguna de esas palabras aparece en "
            "los documentos; prueba con otras, o cambia a híbrida o vectorial."
        )
    if mode == "vector":
        return shared + (
            "En modo vectorial significa que nada del expediente se parece lo "
            "suficiente, según el suelo de similitud configurado. Prueba en modo "
            "híbrido, o baja IEP_RETRIEVAL_MIN_SIMILARITY."
        )
    return shared + (
        "En modo híbrido significa que ni las palabras aparecen en los documentos "
        "ni hay nada semánticamente parecido por encima del suelo configurado."
    )


def _nothing_to_ground(
    request: RagQuestion,
    because: str,
    *,
    withheld_hostile: int = 0,
    withheld_directive: int = 0,
) -> RagAnswer:
    """An answer for the cases where no model was called, and why.

    Three of them - the search matched nothing, every match was in a flagged
    document, every match carried a directive - and none is a server error: a
    retry changes none of them, and what happened is a fact about the
    documents worth telling the reviewer. They also cost nothing, which is the
    other reason not to call a model just to be told the obvious.
    """
    return RagAnswer(
        question=request.question,
        answer=because,
        sufficient_evidence=False,
        citations=[],
        withheld_hostile_segments=withheld_hostile,
        withheld_directive_segments=withheld_directive,
        retrieval_mode=request.retrieval_mode,
        generation_provider="ninguno",
        generation_model="no se ha llamado a ningún modelo",
        prompt_version=RAG_PROMPT_VERSION,
        prompt_sha256="0" * 64,
    )


def _usage_report(session: Session, settings: Settings) -> UsageReport:
    spent = rag_usage.measured(session, settings)
    signed_in = rag_usage.account("claude")
    return UsageReport(
        cloud_calls=spent.cloud_calls,
        cloud_tokens=spent.cloud_tokens,
        local_calls=spent.local_calls,
        local_tokens=spent.local_tokens,
        seconds_until_reset=spent.seconds_until_reset,
        by_model=[
            {
                "model": row.model,
                "calls": row.calls,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
            }
            for row in spent.by_model
        ],
        account_tool=signed_in.tool,
        account_logged_in=signed_in.logged_in,
        account_method=signed_in.method,
        account_plan=signed_in.plan,
        account_detail=signed_in.detail,
    )
