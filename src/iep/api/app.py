"""FastAPI application factory.

The OpenAPI document is the demonstrable interface of this system: everything
a reviewer or an automation engine can do is described there, including the
error shape.

Which is why everything OpenAPI publishes is in Spanish - this page, the tag
descriptions, and every route's `summary` and docstring - while the rest of
the codebase keeps its comments and docstrings in English. The line is not
about taste: a route handler's docstring *is* the endpoint's description, so
it is read by whoever uses the API, and the domain, the documents and the
people who would use this are Spanish. Route paths, field paths and rule ids
stay in English because they are keys rather than prose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from iep import __version__
from iep.api.errors import correlation_middleware, register_error_handlers
from iep.api.routes import artifacts, dossiers, jobs, review, system, ui
from iep.config import get_settings
from iep.domain.contracts import ApiError
from iep.logging import configure_logging

DESCRIPTION = """
Revisión de expedientes de ayudas a la innovación, con cada dato ligado a la
evidencia de la que salió.

Los documentos se dan de alta, se les extraen los datos guardando el sitio
exacto del que salió cada uno —la página, la celda o la caja de palabras del
escaneo—, se cruzan entre sí con reglas deterministas, y todo lo que el
sistema no puede resolver por su cuenta se deriva a una persona. Aquí nada
aprueba un expediente: aprobar es una acción humana, y queda registrada en una
auditoría que sólo admite añadir.

### El camino más corto que sirve para algo

1. `POST /dossiers` — crear el expediente.
2. `POST /dossiers/{id}/documents` — una llamada por fichero.
3. `POST /dossiers/{id}/process` — encolar el trabajo; lo recoge un worker.
4. `GET /jobs/{id}` — seguirlo, o simplemente esperar un segundo.
5. `GET /dossiers/{id}/findings` — a qué han llegado las reglas.
6. `GET /ui/dossiers/{id}` — lo mismo, como una pantalla en la que actuar.

Todas las respuestas de error tienen la misma forma (`error`, `message`,
`correlation_id` y, cuando aplica, `detail`) y nunca una traza. El
`correlation_id` aparece en los registros de la API y del worker, así que una
petición se puede seguir de punta a punta.

La documentación funcional está indexada en `docs/es/README.md`; la versión en
inglés está en `docs/README.md`.
"""

TAGS = [
    {
        "name": "system",
        "description": (
            "Si el proceso está vivo, si además puede trabajar, y sus "
            "contadores. `/healthz` y `/readyz` están separados a propósito: "
            "confundirlos hace que un orquestador reinicie un proceso sano "
            "porque la base de datos ha pestañeado."
        ),
    },
    {
        "name": "dossiers",
        "description": (
            "Crear un expediente, meterle documentos y pedir que se procese. "
            "Cada fichero se comprueba por tamaño, por firma y abriéndolo con "
            "su analizador de verdad **antes** de guardar nada; de un fichero "
            "rechazado queda constancia igualmente, con su motivo, para que "
            "quien revisa vea qué se entregó."
        ),
    },
    {
        "name": "jobs",
        "description": (
            "La cola de procesamiento. Un trabajo que falla sigue a la vista "
            "con su error y su número de intentos: `FAILED` y `DEAD_LETTER` "
            "son estados que se pueden inspeccionar, no un descarte en "
            "silencio."
        ),
    },
    {
        "name": "review",
        "description": (
            "Lo que hace una persona. Leer los campos extraídos y las "
            "incidencias, corregir o confirmar un valor, aceptar o descartar "
            "una incidencia y, al final, aprobar o rechazar. Cada acción "
            "exige quién la hace y por qué, y una corrección nunca borra lo "
            "que leyó la máquina."
        ),
    },
    {
        "name": "artifacts",
        "description": (
            "Lo que te llevas: el informe en HTML y en PDF, las "
            "exportaciones en JSON y CSV, la auditoría que sólo admite "
            "añadir, la búsqueda de evidencia —léxica, vectorial o híbrida— y "
            "un punto de integración opcional, de sólo lectura, que responde "
            "citando la evidencia."
        ),
    },
    {
        "name": "ui",
        "description": (
            "Una pantalla de revisión mínima, renderizada en el servidor. "
            "Existe para poder mirar la evidencia sin montar un cliente; no "
            "es el front de un producto. Se empieza en `GET /ui/dossiers`."
        ),
    },
]


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings.storage_root.mkdir(parents=True, exist_ok=True)
        settings.report_root.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(
        title="Alder API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        # Every failure a client can see has one shape, and the OpenAPI
        # document says so: an integrator should not have to discover the
        # error contract by provoking it.
        responses={
            status_code: {"model": ApiError, "description": description}
            for status_code, description in (
                (400, "The request was understood but not acceptable."),
                (401, "A valid API key is required."),
                (404, "No such resource."),
                (409, "The request conflicts with the current state."),
                (413, "The upload exceeds the configured limit."),
                (422, "The request or the submitted document failed validation."),
                (500, "Unexpected failure. Quote the correlation id."),
                (503, "An optional provider is disabled or temporarily unavailable."),
            )
        },
    )
    app.middleware("http")(correlation_middleware)
    register_error_handlers(app)
    app.include_router(system.router)
    app.include_router(dossiers.router)
    app.include_router(jobs.router)
    app.include_router(review.router)
    app.include_router(artifacts.router)
    app.include_router(ui.router)
    return app


app = create_app()
