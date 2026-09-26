"""Wipe the public demo database when its data is older than --max-age-hours,
so the seed step that follows loads a fresh copy. See app/seed/reset.py.

Runs only when PO_DEMO_MODE is on, and refuses to touch a database holding
any non-demo organization.

Usage:
    uv run python scripts/reset_demo.py [--max-age-hours 24]
"""

from __future__ import annotations

import argparse
from datetime import timedelta

from sqlalchemy import create_engine

from app.core.clock import SystemClock
from app.core.config import load_settings
from app.seed.reset import reset_demo_if_stale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    args = parser.parse_args()

    settings = load_settings()
    if not settings.demo_mode:
        print("demo reset: PO_DEMO_MODE is off; skipped")
        return
    engine = create_engine(settings.database_url)
    try:
        decision = reset_demo_if_stale(
            engine, now=SystemClock().now(), max_age=timedelta(hours=args.max_age_hours)
        )
    finally:
        engine.dispose()
    print(f"demo reset: {decision.reason}")


if __name__ == "__main__":
    main()
