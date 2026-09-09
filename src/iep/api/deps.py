"""Request-scoped dependencies."""

from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from iep.api.errors import UnauthorizedError
from iep.config import Settings, get_settings
from iep.db.session import get_session_factory
from iep.storage.base import ObjectStore
from iep.storage.local import LocalObjectStore


def settings_dep() -> Settings:
    return get_settings()


def db_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def object_store(settings: Settings = Depends(settings_dep)) -> ObjectStore:
    return LocalObjectStore(settings.storage_root)


def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(settings_dep),
) -> None:
    """Optional shared-secret gate.

    This is a deployment guard, not authentication: there is no user identity
    and no authorisation model behind it. See docs/threat-model.md, which
    records that as an accepted gap rather than a solved problem.
    """
    if not settings.api_key:
        return
    if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
        raise UnauthorizedError("A valid X-API-Key header is required.")


def correlation(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    return value if isinstance(value, str) else "unknown"
