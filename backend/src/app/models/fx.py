"""Exchange rates: currency normalization inputs (docs/planning/02-erd.md
EXCHANGE_RATES, SPEC §9, docs/planning/05-calculation-methodology.md §4).

Org-owned, append-only history: no `updated_at`/`version`/`archived_at` (the
ERD lists none for this table), the same judgement call already made for
`SupplierPerformanceRecord` (backend/src/app/models/suppliers.py) — a row is a
historical fact (a rate observed or set as-of a date), never edited in place.
Superseding a rate means inserting a new row with a later `effective_date` (or
a manual override for the same date), not mutating an old one; §4's rule "the
scenario stores the rate id and value; later edits to rates never change
historical results" depends on that immutability.

Only manual-override rows are ever persisted here in v0.1. The synthetic
fixture provider (`app.providers.fx.synthetic.SyntheticFxProvider`) computes
rates purely functionally and is never written to this table — `fx_service`
returns its answer transparently in API responses without an INSERT (see
that module's docstring). The ERD's `source "fixture|manual|provider_name"`
comment is broader than what v0.1 exercises: `provider_name`/`fixture` rows
would come from a future live-provider refresh job, not from anything in this
phase.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CHAR, Boolean, Date, ForeignKey, Numeric, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import OrgOwnedBase, org_identity_constraint


class ExchangeRate(OrgOwnedBase):
    """`1 base_currency = rate * quote_currency` (05-calculation-methodology.md
    §4's standard direct quotation). Manual overrides win over any other row
    for the same `(organization_id, base_currency, quote_currency,
    effective_date)` — enforced by `uq_exchange_rate_natural` (02-erd.md §8)
    together with `fx_service.get_effective_rate`'s selection order, not by a
    CHECK (a CHECK cannot express "wins over").

    `override_reason` is required whenever `is_manual_override` is true
    (`ck_exchange_rates_override_reason_required`) — SPEC §9 lists "manual
    override, override reason" as fields every rate preserves, so a flagged
    override with no reason is a data error, not a valid state.
    """

    __tablename__ = "exchange_rates"
    __table_args__ = (org_identity_constraint("exchange_rates"),)

    base_currency: Mapped[str] = mapped_column(CHAR(3))
    quote_currency: Mapped[str] = mapped_column(CHAR(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(24, 12))
    effective_date: Mapped[date] = mapped_column(Date())
    source: Mapped[str] = mapped_column(Text())  # "manual" in v0.1; see module docstring
    is_manual_override: Mapped[bool] = mapped_column(Boolean(), default=False)
    override_reason: Mapped[str | None] = mapped_column(Text(), default=None)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    retrieved_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column()


# UOW insert-ordering relationships (see identity.py comment).
ExchangeRate.organization = relationship("Organization", lazy="select")
ExchangeRate.created_by = relationship(
    "User", foreign_keys=[ExchangeRate.created_by_id], lazy="select"
)


__all__ = ["ExchangeRate"]
