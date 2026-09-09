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
"""


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
