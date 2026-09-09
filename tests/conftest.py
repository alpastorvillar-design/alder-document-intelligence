"""Shared fixtures.

Integration tests talk to a real PostgreSQL because most of what is worth
testing here is database behaviour: SKIP LOCKED, unique constraints, conditional
updates, generated tsvector columns. A SQLite substitute would pass while
proving nothing about any of them.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from iep.config import Settings, get_settings
from iep.db import session as db_session_module
from iep.db.models import Base
from iep.storage.local import LocalObjectStore

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://iep:iep@localhost:55432/iep"


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("IEP_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get("IEP_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@pytest.fixture(scope="session")
def engine(database_url: str):  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable at {database_url}: {type(exc).__name__}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def settings(tmp_path: Path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        storage_root=tmp_path / "objects",
        report_root=tmp_path / "reports",
        semantic_provider="deterministic",
        scraper_allowlist="localhost,127.0.0.1,devsources",
        worker_poll_seconds=0.01,
        job_lease_seconds=2,
    )


@pytest.fixture
def store(settings: Settings) -> LocalObjectStore:
    return LocalObjectStore(settings.storage_root)


@pytest.fixture
def db(engine) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    """A session on a clean set of tables.

    Truncation rather than a rolled-back transaction: several tests need two
    connections to see each other's committed rows, which a single outer
    transaction would prevent.
    """
    _truncate(engine)
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def session_factory(engine):  # type: ignore[no-untyped-def]
    """Factory for tests that need several independent sessions."""
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def wired_settings(settings: Settings, engine, monkeypatch: pytest.MonkeyPatch) -> Settings:  # type: ignore[no-untyped-def]
    """Point the process-wide settings and engine at the test fixtures."""
    get_settings.cache_clear()
    monkeypatch.setenv("IEP_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("IEP_STORAGE_ROOT", str(settings.storage_root))
    monkeypatch.setenv("IEP_REPORT_ROOT", str(settings.report_root))
    monkeypatch.setenv("IEP_SCRAPER_ALLOWLIST", settings.scraper_allowlist)
    monkeypatch.setattr(db_session_module, "_engine", engine, raising=False)
    monkeypatch.setattr(db_session_module, "_session_factory", None, raising=False)
    yield get_settings()
    get_settings.cache_clear()


def _truncate(engine) -> None:  # type: ignore[no-untyped-def]
    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(scope="session")
def corpus_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The generated corpus, built once per test session."""
    from corpus.generate import write_corpus

    target = tmp_path_factory.mktemp("corpus")
    write_corpus(target)
    return target


@pytest.fixture(scope="session")
def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


@pytest.fixture
def requires_ocr(tesseract_available: bool) -> None:
    if not tesseract_available:
        pytest.skip("Tesseract is not installed on this machine")


def new_dossier(session: Session, reference: str | None = None):  # type: ignore[no-untyped-def]
    """A DRAFT dossier with sane defaults, for tests that need one."""
    from datetime import date
    from decimal import Decimal

    from iep.domain.contracts import DossierCreate
    from iep.dossiers import service as dossiers

    reference = reference or f"INN-2025-{uuid.uuid4().int % 900 + 100:03d}"
    return dossiers.create(
        session,
        DossierCreate(
            reference=reference,
            title="Test dossier",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            claimed_total_eur=Decimal("1000.00"),
        ),
    )
