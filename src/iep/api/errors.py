"""Error handling.

One rule: a client never receives an internal detail. Every unexpected
exception becomes a generic 500 carrying only a correlation id; the traceback
goes to the log, where the same correlation id makes it findable.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from iep.domain.contracts import ApiError
from iep.logging import log_context, safe_extra

CORRELATION_HEADER = "X-Correlation-ID"

log = logging.getLogger(__name__)


class DomainError(Exception):
    """Base for errors that are safe to describe to a caller."""

    status_code = 400
    error_code = "domain_error"

    def __init__(self, message: str, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class NotFoundError(DomainError):
    status_code = 404
    error_code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    error_code = "conflict"


class UnprocessableDocumentError(DomainError):
    status_code = 422
    error_code = "unprocessable_document"


class PayloadTooLargeError(DomainError):
    status_code = 413
    error_code = "payload_too_large"


class UnauthorizedError(DomainError):
    status_code = 401
    error_code = "unauthorized"


class ServiceUnavailableError(DomainError):
    status_code = 503
    error_code = "service_unavailable"


class InvalidStateTransitionError(ConflictError):
    error_code = "invalid_state_transition"


def correlation_id(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else "unknown"


async def correlation_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # A caller-supplied id is echoed so a workflow engine can stitch its run to
    # our logs; it is length-capped because it ends up in every log line.
    incoming = request.headers.get(CORRELATION_HEADER, "")[:64].strip()
    cid = incoming or uuid.uuid4().hex
    request.state.correlation_id = cid
    with log_context(correlation_id=cid, path=request.url.path, method=request.method):
        response = await call_next(request)
    response.headers[CORRELATION_HEADER] = cid
    return response


def _payload(request: Request, code: str, message: str, detail: object = None) -> JSONResponse:
    body = ApiError(
        error=code,
        message=message,
        correlation_id=correlation_id(request),
        detail=detail if isinstance(detail, dict) else None,
    )
    status = getattr(request.state, "_error_status", 400)
    return JSONResponse(status_code=status, content=body.model_dump())


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(request: Request, exc: DomainError) -> JSONResponse:
        request.state._error_status = exc.status_code
        log.info(
            "domain_error",
            extra=safe_extra({"error_code": exc.error_code, "message": exc.message}),
        )
        return _payload(request, exc.error_code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        request.state._error_status = 422
        # Pydantic's error list is about the caller's own request, so it is safe
        # to return; it is serialised through jsonable defaults to drop any
        # non-JSON objects it may carry.
        return _payload(
            request,
            "validation_error",
            "The request body or parameters failed validation.",
            {"errors": [{k: str(v) for k, v in e.items() if k != "ctx"} for e in exc.errors()]},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request.state._error_status = exc.status_code
        return _payload(request, f"http_{exc.status_code}", str(exc.detail))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request.state._error_status = 500
        log.exception("unhandled_exception", extra={"error_type": type(exc).__name__})
        return _payload(
            request,
            "internal_error",
            "The request could not be completed. Quote the correlation id when reporting this.",
        )
