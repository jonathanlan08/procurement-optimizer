"""Periodic reset of the public demo database.

The hosted demo is open to anyone with the published demo credentials, so
visitors can create, edit and archive records. Once the demo data is older than
a limit (a day, by default) the next server start wipes every table and the
normal seed step then loads a fresh copy.

Wiping is refused unless EVERY organization in the database is a demo
organization: a database holding any real tenant is never touched. It also
has to lift the audit log's append-only guard (migration 0001) for the length
of one transaction - that guard protects real tenants' history, and the demo's
history is exactly what this reset exists to discard.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.models.identity import Organization

KEEP_TABLES = frozenset({"alembic_version"})
AUDIT_GUARD_TRIGGERS = ("trg_audit_events_append_only", "trg_audit_events_no_truncate")


@dataclass(frozen=True)
class ResetDecision:
    reset: bool
    reason: str


def decide_reset(
    orgs: Sequence[tuple[bool, datetime]], *, now: datetime, max_age: timedelta
) -> ResetDecision:
    """`orgs` is (is_demo, created_at) for every organization in the database."""
    if not orgs:
        return ResetDecision(False, "no organizations yet; nothing to reset")
    if not all(is_demo for is_demo, _ in orgs):
        return ResetDecision(False, "a non-demo organization exists; refusing to wipe")
    age = now - min(created_at for _, created_at in orgs)
    if age < max_age:
        return ResetDecision(False, f"demo data is {_hours(age)} old; keeping it")
    return ResetDecision(True, f"demo data is {_hours(age)} old; wiped for a fresh seed")


def wipe_all_tables(engine: Engine) -> list[str]:
    """Truncate every public table except the migration marker, in one
    transaction, so a failure leaves both the data and the audit guard intact."""
    with engine.begin() as conn:
        names = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        targets = sorted(name for name in names if name not in KEEP_TABLES)
        if not targets:
            return []
        for trigger in AUDIT_GUARD_TRIGGERS:
            conn.execute(text(f"ALTER TABLE audit_events DISABLE TRIGGER {trigger}"))
        quoted = ", ".join(f'"{name}"' for name in targets)
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
        for trigger in AUDIT_GUARD_TRIGGERS:
            conn.execute(text(f"ALTER TABLE audit_events ENABLE TRIGGER {trigger}"))
    return targets


def reset_demo_if_stale(engine: Engine, *, now: datetime, max_age: timedelta) -> ResetDecision:
    with Session(engine) as session:
        orgs = [
            (row.is_demo, row.created_at)
            for row in session.execute(select(Organization.is_demo, Organization.created_at))
        ]
    decision = decide_reset(orgs, now=now, max_age=max_age)
    if decision.reset:
        wipe_all_tables(engine)
    return decision


def _hours(age: timedelta) -> str:
    return f"{age.total_seconds() / 3600:.1f}h"
