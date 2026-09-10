"""FastAPI application factory.

The OpenAPI document is the demonstrable interface of this system: everything
a reviewer or an automation engine can do is described there, including the
error shape.
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
Evidence-linked review of innovation funding dossiers.

Documents are ingested, extracted with a locator back to the page, cell or word
box they came from, cross-checked by deterministic rules, and routed to a human
whenever the system cannot settle a question on its own. Nothing here approves
a dossier: approval is a human action recorded in an append-only audit trail.

### The shortest useful path

1. `POST /dossiers` — create the dossier.
2. `POST /dossiers/{id}/documents` — one call per file.
3. `POST /dossiers/{id}/process` — enqueue the work; a worker picks it up.
4. `GET /jobs/{id}` — watch it, or just wait a second.
5. `GET /dossiers/{id}/findings` — what the rules concluded.
6. `GET /ui/dossiers/{id}` — the same thing as a page a person can act on.

Every error response has one shape (`error`, `message`, `correlation_id`,
optional `detail`) and never a stack trace. The correlation id appears in the
API and worker logs, so one request can be followed end to end.

A guided tour of all of this is in `docs/walkthrough.md`; the same document in
Spanish is `docs/es/recorrido.md`.
"""

TAGS = [
    {
        "name": "system",
        "description": (
            "Is the process alive, can it work, and what are its counters. "
            "`/healthz` and `/readyz` are deliberately separate: conflating "
            "them makes an orchestrator restart a healthy process because the "
            "database blinked."
        ),
    },
    {
        "name": "dossiers",
        "description": (
            "Create a dossier, put documents in it, and ask for it to be "
            "processed. A file is checked by size, by signature and by opening "
            "it with its real parser **before** anything is stored; a refused "
            "file is still recorded, with its reason, so a reviewer sees what "
            "was submitted."
        ),
    },
    {
        "name": "jobs",
        "description": (
            "The processing queue. A failed job stays visible with its error "
            "and attempt count: `FAILED` and `DEAD_LETTER` are inspectable "
            "states, not a silent drop."
        ),
    },
    {
        "name": "review",
        "description": (
            "What a person does. Read the extracted fields and the findings, "
            "correct or confirm a value, accept or dismiss a finding, and "
            "finally approve or reject. Every action needs an actor and a "
            "reason, and a correction never erases what the machine read."
        ),
    },
    {
        "name": "artifacts",
        "description": (
            "What you take away: the HTML report, JSON and CSV exports, the "
            "append-only audit trail, lexical/vector/hybrid evidence search, "
            "and an optional read-only grounded-answer boundary."
        ),
    },
    {
        "name": "ui",
        "description": (
            "A minimal server-rendered review screen. It exists so the "
            "evidence can be looked at without a client; it is not a product "
            "front end. Start at `GET /ui/dossiers`."
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
        title="Innovation Evidence Pipeline",
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
