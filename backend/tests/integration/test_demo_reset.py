"""Periodic demo reset (app/seed/reset.py).

Runs against its own throwaway database in the test cluster: a wipe here must
never touch the shared, session-scoped test database other tests rely on.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from app.core.clock import SystemClock
from app.core.config import Settings
from app.core.ids import RandomIdGenerator
from app.models.identity import Organization
from app.providers.extraction.mock import MockExtractionProvider
from app.providers.storage import build_storage_provider
from app.seed.demo_dataset import seed_demo_dataset
from app.seed.reset import AUDIT_GUARD_TRIGGERS, decide_reset, reset_demo_if_stale

BACKEND_DIR = Path(__file__).resolve().parents[2]
DAY = timedelta(hours=24)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


class TestDecision:
    def test_empty_database_is_left_alone(self) -> None:
        assert not decide_reset([], now=NOW, max_age=DAY).reset

    def test_fresh_demo_data_is_kept(self) -> None:
        orgs = [(True, NOW - timedelta(hours=3))]
        assert not decide_reset(orgs, now=NOW, max_age=DAY).reset

    def test_stale_demo_data_is_reset(self) -> None:
        orgs = [(True, NOW - timedelta(hours=25))]
        assert decide_reset(orgs, now=NOW, max_age=DAY).reset

    def test_any_real_organization_blocks_the_wipe(self) -> None:
        orgs = [(True, NOW - timedelta(days=9)), (False, NOW - timedelta(days=9))]
        decision = decide_reset(orgs, now=NOW, max_age=DAY)
        assert not decision.reset
        assert "non-demo" in decision.reason


@pytest.fixture()
def scratch_engine(
    database_url: str, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Generator[Engine, None, None]:
    from alembic import command
    from alembic.config import Config

    name = f"demo_reset_{uuid.uuid4().hex[:8]}"
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    # swap only the database name: re-rendering through make_url percent-encodes
    # a socket-path host, which Alembic's configparser rejects as interpolation
    base, sep, query = database_url.partition("?")
    url = f"{base.rsplit('/', 1)[0]}/{name}{sep}{query}"
    monkeypatch.setenv("PO_DATABASE_URL", url)
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _seed(engine: Engine, storage_root: Path) -> None:
    settings = Settings(storage_root=str(storage_root))
    with Session(engine) as session:
        seed_demo_dataset(
            session,
            clock=SystemClock(),
            ids=RandomIdGenerator(),
            storage=build_storage_provider(settings),
            extraction_provider=MockExtractionProvider(),
        )
        session.commit()


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


class TestReset:
    def test_stale_demo_is_wiped_then_reseeds_cleanly(
        self, scratch_engine: Engine, tmp_path: Path
    ) -> None:
        _seed(scratch_engine, tmp_path)
        assert _count(scratch_engine, "organizations") == 1
        assert _count(scratch_engine, "audit_events") > 0
        now = datetime.now(UTC)

        kept = reset_demo_if_stale(scratch_engine, now=now, max_age=DAY)
        assert not kept.reset
        assert _count(scratch_engine, "suppliers") > 0

        wiped = reset_demo_if_stale(scratch_engine, now=now + DAY + DAY, max_age=DAY)
        assert wiped.reset
        for table in ("organizations", "users", "suppliers", "audit_events", "sessions"):
            assert _count(scratch_engine, table) == 0, table
        assert _count(scratch_engine, "alembic_version") == 1

        _seed(scratch_engine, tmp_path / "again")
        assert _count(scratch_engine, "organizations") == 1

    def test_audit_append_only_guard_is_back_on_after_a_wipe(
        self, scratch_engine: Engine, tmp_path: Path
    ) -> None:
        _seed(scratch_engine, tmp_path)
        reset_demo_if_stale(scratch_engine, now=datetime.now(UTC) + DAY + DAY, max_age=DAY)
        with scratch_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tgname, tgenabled FROM pg_trigger"
                    " WHERE tgrelid = 'audit_events'::regclass AND NOT tgisinternal"
                )
            ).all()
        states: dict[str, str] = {str(name): str(state) for name, state in rows}
        enabled = dict.fromkeys(AUDIT_GUARD_TRIGGERS, "O")
        assert {name: states.get(name) for name in AUDIT_GUARD_TRIGGERS} == enabled

    def test_a_database_with_a_real_organization_is_never_wiped(
        self, scratch_engine: Engine, tmp_path: Path
    ) -> None:
        _seed(scratch_engine, tmp_path)
        now = datetime.now(UTC)
        with Session(scratch_engine) as session:
            session.add(
                Organization(
                    id=uuid.uuid4(),
                    slug="real-customer",
                    name="Real Customer Inc",
                    base_currency="USD",
                    is_demo=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        decision = reset_demo_if_stale(scratch_engine, now=now + DAY + DAY, max_age=DAY)
        assert not decision.reset
        assert _count(scratch_engine, "organizations") == 2
        assert _count(scratch_engine, "suppliers") > 0
