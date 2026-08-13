"""Engine and session management - PRINCIPAL-OWNED.

Transaction rule (docs/planning/01-architecture.md §5): one HTTP request = at most
one DB transaction, opened and committed by the application service via the unit
of work. Repositories never commit.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session as SaSession
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings


def build_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )


def build_session_factory(engine: Engine) -> sessionmaker[SaSession]:
    return sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )


@contextmanager
def unit_of_work(session_factory: sessionmaker[SaSession]) -> Generator[SaSession, None, None]:
    """One transaction: commit on success, rollback on any exception."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
