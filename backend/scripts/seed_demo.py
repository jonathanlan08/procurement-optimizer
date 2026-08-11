"""Seed the synthetic demonstration organization (idempotent).

Thin CLI wrapper around `app.seed.demo_dataset.seed_demo_dataset`, which owns
the actual dataset (org/users, suppliers, parts, BOMs, RFQs, quotes, FX
overrides, scoring configurations, scenarios, documents + extraction — see
that module's docstring for the full inventory and the SPEC checklist items
each piece demonstrates). ALL DATA IS SYNTHETIC.

Usage:
    export PO_DATABASE_URL=...   # from scripts/dev_db.py
    uv run python scripts/seed_demo.py
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SaSession

from app.core.clock import SystemClock
from app.core.config import load_settings
from app.core.ids import RandomIdGenerator
from app.providers.extraction.mock import MockExtractionProvider
from app.providers.storage import build_storage_provider
from app.seed.demo_dataset import DemoDatasetSummary, seed_demo_dataset


def _print_summary(summary: DemoDatasetSummary) -> None:
    print(f"organization: {summary.organization_id}")
    print(f"users: {len(summary.user_ids)}")
    print(f"suppliers: {len(summary.supplier_ids)}")
    print(f"parts: {len(summary.part_ids)}")
    print(f"BOM versions: {len(summary.bom_ids)}")
    print(f"RFQs: {len(summary.rfq_ids)}")
    print(f"quotes: {len(summary.quote_ids)}")
    print(f"scenarios: {len(summary.scenario_ids)}")
    print(f"scoring configurations: {len(summary.scoring_configuration_ids)}")
    print(f"documents: {len(summary.document_ids)}")
    print(f"exchange rate overrides: {len(summary.exchange_rate_ids)}")
    print(f"extraction run: {summary.extraction_run_id}")


def seed() -> DemoDatasetSummary:
    settings = load_settings()
    engine = create_engine(settings.database_url)
    clock = SystemClock()
    ids = RandomIdGenerator()
    storage = build_storage_provider(settings)
    extraction_provider = MockExtractionProvider()

    with SaSession(engine) as session:
        summary = seed_demo_dataset(
            session,
            clock=clock,
            ids=ids,
            storage=storage,
            extraction_provider=extraction_provider,
            max_upload_bytes=settings.max_upload_bytes,
        )
        session.commit()
    engine.dispose()
    return summary


if __name__ == "__main__":
    result = seed()
    _print_summary(result)
    print("seed complete")
