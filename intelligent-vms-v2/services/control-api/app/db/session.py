"""Engine and session management.

Concurrency model (ADR-0005): the control API uses SYNCHRONOUS SQLAlchemy
sessions executed in FastAPI's worker threadpool. Handlers that touch the
database are declared `def`, not `async def`. Rationale: the sync ORM is the
mature path, media data never flows through this process, and request rates
for a control plane are low (hundreds/minute).

Resilience:
  * pool_pre_ping detects a restarted PostgreSQL and reconnects transparently.
  * wait_for_database() retries with bounded backoff at startup so a server
    rebooting alongside its database comes up cleanly.
  * session_scope() gives an explicit commit/rollback boundary.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.core.backoff import BackoffPolicy
from app.logging_setup import get_logger

log = get_logger("db")

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}

    if url.startswith("sqlite"):
        # check_same_thread=False: the threadpool hands connections between threads.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs.update(pool_size=settings.database_pool_size,
                      max_overflow=settings.database_max_overflow,
                      pool_recycle=settings.database_pool_recycle_s, pool_timeout=30)

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cur = dbapi_connection.cursor()
            # WAL keeps playback queries from blocking recorder writes;
            # foreign keys are OFF by default in SQLite.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        _engine = build_engine(settings)
        _SessionFactory = sessionmaker(bind=_engine, autoflush=False,
                                       expire_on_commit=False, future=True)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    get_engine(settings)
    assert _SessionFactory is not None
    return _SessionFactory


def dispose_engine() -> None:
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def set_engine(engine: Engine) -> None:
    """Injection hook for tests and for workers that build their own engine."""
    global _engine, _SessionFactory
    _engine = engine
    _SessionFactory = sessionmaker(bind=engine, autoflush=False,
                                   expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on any exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def db_dependency() -> Iterator[Session]:
    """FastAPI dependency. Handlers must be `def`, not `async def`."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def wait_for_database(settings: Settings | None = None,
                      policy: BackoffPolicy | None = None, sleep=time.sleep) -> bool:
    """Block until the database answers, with bounded retries.

    Raises the last OperationalError when the budget is exhausted; startup
    treats that as a loud fatal failure rather than silently running without
    persistence.
    """
    settings = settings or get_settings()
    policy = policy or BackoffPolicy(initial_s=0.5,
                                     max_s=settings.database_connect_backoff_s * 8,
                                     max_attempts=settings.database_connect_retries)
    engine = get_engine(settings)
    last: Exception | None = None
    for attempt in range(1, max(1, policy.max_attempts) + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                log.info("database.reconnected", attempt=attempt)
            return True
        except OperationalError as exc:
            last = exc
            delay = policy.delay_for(attempt)
            log.warning("database.unavailable", attempt=attempt,
                        retry_in_s=round(delay, 2), error=str(exc)[:200])
            sleep(delay)
    raise last if last else RuntimeError("database unavailable")


def healthcheck() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
