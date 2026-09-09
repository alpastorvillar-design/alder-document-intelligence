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
from iep.api.routes import system
from iep.config import get_settings
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
    )
    app.middleware("http")(correlation_middleware)
    register_error_handlers(app)
    app.include_router(system.router)
    return app


app = create_app()
