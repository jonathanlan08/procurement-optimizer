"""Test infrastructure — PRINCIPAL-OWNED.

Database resolution order (docs/planning/09 task 1.18):
1. PO_TEST_DATABASE_URL if set (CI service container, developer's own postgres)
2. pgserver — real user-space PostgreSQL, no Docker required

Integration tests are marked `integration`; pure unit tests never touch this.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _pgserver_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    import pgserver

    datadir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(datadir), cleanup_mode="stop")
    # translate pgserver's URI into SQLAlchemy+psycopg form
    uri = server.get_uri()  # postgresql://postgres:@/postgres?host=/sock/dir
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    env_url = os.environ.get("PO_TEST_DATABASE_URL")
    if env_url:
        return env_url
    return _pgserver_url(tmp_path_factory)


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Generator[Engine, None, None]:
    """Engine against a database migrated to head once per test session."""
    from alembic import command
    from alembic.config import Config

    os.environ["PO_DATABASE_URL"] = database_url
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    command.upgrade(cfg, "head")

    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(migrated_engine: Engine) -> Generator[object, None, None]:
    """A session wrapped in an outer transaction that is always rolled back,
    so tests cannot leak state into each other."""
    from sqlalchemy.orm import Session as SaSession

    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = SaSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def make_uuid() -> object:
    from app.core.ids import SequentialIdGenerator

    return SequentialIdGenerator()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-mark tests under tests/integration as integration."""
    for item in items:
        if "tests/integration" in str(item.path):
            item.add_marker(pytest.mark.integration)


__all__ = ["text", "uuid"]
