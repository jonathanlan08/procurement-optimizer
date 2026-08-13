"""quote_lines.documentation_cost / quote_lines.handling_cost (2026-08
product-audit remediation: "Add documentation and handling inputs").

**Why now, and why these two exactly.** `app.domain.landed_cost.contracts`
(frozen) has always accepted a `documentation` field on
`FixedCosts` and a `handling` field on `LogisticsCosts` - the calculation
formulas in that module's own docstring have always read
`allocated_fixed_cost = tooling + setup + documentation + other_fixed` and
`logistics_cost = shipping + insurance + packaging + handling`. What never
existed was a *source* for either: `quote_lines` carries a column for every
other additive fixed/logistics amount (`tooling_cost`, `setup_cost`,
`packaging_cost`, `shipping_cost`, `insurance_cost`, `other_fixed_cost`) but
not these two, so `LandedCostService._assemble_input` has always passed them
as a hardcoded `Quantified.missing(...)` regardless of what the quote line
actually says - see that service's own module docstring and
`docs/METHODOLOGY.md` §7 ("Why `COMPLETE` completeness is structurally
unreachable in v0.1") for the consequence: every persisted result was
either `INCOMPLETE` or `ASSUMPTION_DEPENDENT`, never `COMPLETE`, because
these two components could never be anything but missing. This migration
closes that gap at the only layer that was actually missing it - the domain
calculator required no change at all, confirmed by reading
`app/domain/landed_cost/{contracts,calculator}.py` first, per the
own instruction, before writing a single line here.

Both columns follow the sibling fixed/logistics-cost columns' shape
EXACTLY: `NUMERIC(18, 6)`, nullable (NULL = "not stated", never coerced to
zero - same "missing stays missing" rule `app/schemas/quotes.py`'s module
docstring states for every other commercial field on a quote line), with a
`... IS NULL OR ... >= 0` CHECK constraint mirroring
`ck_quote_lines_packaging_cost_nonneg`/`ck_quote_lines_other_fixed_cost_nonneg`
(migration 0009) verbatim.

Deliberately NOT touched here (see `services/extraction_service.py` and
`services/landed_cost_service.py` module docstrings for the full reasoning,
repeated briefly): the extraction payload schema is versioned and documents
essentially never state a standalone "documentation cost" or "handling
cost" line item, so these two columns are populated by manual entry
(`QuoteLineCreate`) and correction only, exactly like every other
commercial field a supplier's raw document doesn't carry a slot for.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_lines",
        sa.Column("documentation_cost", sa.Numeric(18, 6), nullable=True),
    )
    op.add_column(
        "quote_lines",
        sa.Column("handling_cost", sa.Numeric(18, 6), nullable=True),
    )
    op.create_check_constraint(
        "ck_quote_lines_documentation_cost_nonneg",
        "quote_lines",
        "documentation_cost IS NULL OR documentation_cost >= 0",
    )
    op.create_check_constraint(
        "ck_quote_lines_handling_cost_nonneg",
        "quote_lines",
        "handling_cost IS NULL OR handling_cost >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_quote_lines_handling_cost_nonneg", "quote_lines", type_="check"
    )
    op.drop_constraint(
        "ck_quote_lines_documentation_cost_nonneg", "quote_lines", type_="check"
    )
    op.drop_column("quote_lines", "handling_cost")
    op.drop_column("quote_lines", "documentation_cost")
