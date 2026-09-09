"""Shared fixtures.

Integration tests talk to a real PostgreSQL because most of what is worth
testing here is database behaviour: SKIP LOCKED, unique constraints, conditional
updates, generated tsvector columns. A SQLite substitute would pass while
proving nothing about any of them.

They never talk to the *application's* database. Every test that needs a clean
slate truncates every table, and pointing that at the database the running
stack serves would silently destroy whatever a demo had just loaded. The
session therefore derives a sibling database - `<name>_test` - from the
configured URL, creates it if it is missing, and refuses to run against a
database whose name does not end in `_test`.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.orm import Session

from iep.config import Settings, get_settings
from iep.db import session as db_session_module
from iep.db.models import Base
from iep.storage.local import LocalObjectStore

# 127.0.0.1 rather than localhost: the published port binds to IPv4 only, and
# resolving localhost tries ::1 first, which costs a connection timeout per
# attempt on a developer machine.
DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://iep:iep@127.0.0.1:55432/iep"
TEST_DATABASE_SUFFIX = "_test"


def pytest_configure(config: pytest.Config) -> None:
    """Point the whole session, environment included, at the test database.

    Code under test that opens its own session - the worker loop, the CLI -
    reads `IEP_DATABASE_URL` rather than a fixture. Leaving that variable on the
    application database would have those paths reading and writing real data
    while the fixtures worked on a different one, which is how a test can pass
    against rows it never created.
    """
    os.environ["IEP_DATABASE_URL"] = as_test_database(
        os.environ.get("IEP_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    )


def as_test_database(url: str) -> str:
    """Point a configured URL at its `_test` sibling."""
    parsed = make_url(url)
    name = parsed.database or "iep"
    if name.endswith(TEST_DATABASE_SUFFIX):
        return url
    # `str(URL)` masks the password as ***; the credential has to survive.
    return parsed.set(database=f"{name}{TEST_DATABASE_SUFFIX}").render_as_string(
        hide_password=False
    )


def _ensure_database_exists(url: str) -> None:
    """Create the test database if the server does not have it yet.

    Connects to the `postgres` maintenance database because CREATE DATABASE
    cannot run inside a transaction or against the database being created.
    """
    from sqlalchemy import create_engine

    parsed = make_url(url)
    target = parsed.database
    assert target and target.endswith(TEST_DATABASE_SUFFIX)

    admin = create_engine(
        parsed.set(database="postgres").render_as_string(hide_password=False),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target}
            ).scalar_one_or_none()
            if exists is None:
                connection.execute(text(f'CREATE DATABASE "{target}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    return as_test_database(os.environ.get("IEP_DATABASE_URL", DEFAULT_TEST_DATABASE_URL))


@pytest.fixture(scope="session")
def engine(database_url: str):  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine

    name = make_url(database_url).database or ""
    if not name.endswith(TEST_DATABASE_SUFFIX):
        # The suite truncates every table. Running it against anything but a
        # dedicated test database would destroy real data.
        pytest.fail(f"refusing to run against {name!r}: the test database must end in _test")

    try:
        _ensure_database_exists(database_url)
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable at {database_url}: {type(exc).__name__}")

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment guard
        pytest.skip(f"PostgreSQL not reachable at {database_url}: {type(exc).__name__}")

    _rebuild_schema(engine)
    yield engine
    engine.dispose()


def _rebuild_schema(engine) -> None:  # type: ignore[no-untyped-def]
    """Build the test schema from nothing, with the migrations.

    Two decisions, both deliberate:

    * the schema is dropped first, so every session starts from an empty
      database and a stale test database cannot silently keep an old shape;
    * it is built by running the migrations rather than `create_all`.
      `create_all` produces whatever the models currently say and never alters
      an existing table, so a model change with no migration would pass the
      suite and fail on deployment. This way the tests exercise the schema a
      deployment actually gets.
    """
    from alembic import command
    from alembic.config import Config

    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))

    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")


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
