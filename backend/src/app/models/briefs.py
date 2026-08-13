"""negotiation_briefs (docs/planning/02-erd.md §7, docs/SPEC.md
§Negotiation brief).

See migration 0014's own module docstring for the full ERD/task-paraphrase
reconciliation (`sections` not `content`, `simulated` not
`narrative_is_generated`, the 3-member `BriefState` ERD enum, and why
`created_by_id`/timestamps/`version`/archivable are present despite the
ERD §7 box not spelling them out for this table specifically).

## `SectionProvenance` - SPEC's own five-label vocabulary, not
`app.domain.values.Provenance`

SPEC §Negotiation brief: "Clearly label: supplier-provided vs
user-assumption vs calculated vs AI narrative vs missing." This is a
distinct, smaller vocabulary from `app.domain.values.Provenance` (six
members, built for landed-cost *component* provenance, e.g.
`DEFAULT`/`USER_INPUT`), and that module is /frozen and not
on the edit allowlist - reusing it here would silently import two
extra members no negotiation-brief section can ever legitimately carry.
Defined here, next to the model that persists sections labelled with it,
rather than in `app/services/brief_service.py`, so `app/schemas/briefs.py`
can import one shared source of truth for validating the wire shape.
`AI_NARRATIVE` is deliberately its own label, distinct from `CALCULATED`:
it marks prose that passed through `AiNarrativeProvider.render_sections`
(free-text, subject to the numeric cross-check), never a bare arithmetic
result.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, ForeignKey, ForeignKeyConstraint, Numeric, Text
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    ArchivableMixin,
    OrgOwnedBase,
    TimestampedMixin,
    VersionedMixin,
    org_identity_constraint,
)


class BriefState(StrEnum):
    DRAFT = "draft"
    HUMAN_REVIEWED = "human_reviewed"
    # ERD-literal member; unreachable via this build's routes (module docstring)
    APPROVED = "approved"


BRIEF_STATE_ENUM = SaEnum(
    BriefState,
    name="brief_state_enum",
    values_callable=lambda e: [m.value for m in e],
)


class SectionProvenance(StrEnum):
    """SPEC §Negotiation brief's five labels - see module docstring."""

    SUPPLIER_PROVIDED = "supplier_provided"
    USER_ASSUMPTION = "user_assumption"
    CALCULATED = "calculated"
    AI_NARRATIVE = "ai_narrative"
    MISSING = "missing"


class NegotiationBrief(TimestampedMixin, VersionedMixin, ArchivableMixin, OrgOwnedBase):
    """One negotiation brief, grounded in one `ComparisonScenario` for one
    scored `Supplier` within it. See module docstring for naming/column
    reconciliation against the ERD box and the paraphrase."""

    __tablename__ = "negotiation_briefs"
    __table_args__ = (
        org_identity_constraint("negotiation_briefs"),
        # composite org FK: the scenario this brief is grounded in.
        ForeignKeyConstraint(
            ["organization_id", "scenario_id"],
            ["comparison_scenarios.organization_id", "comparison_scenarios.id"],
            ondelete="RESTRICT",
            name="fk_negotiation_briefs_organization_id_scenario_id",
        ),
        # composite org FK: the supplier this brief targets.
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            ondelete="RESTRICT",
            name="fk_negotiation_briefs_organization_id_supplier_id",
        ),
    )

    # part of composite FK #1 above, not a single-column ForeignKey
    scenario_id: Mapped[uuid.UUID] = mapped_column()
    # part of composite FK #2 above, not a single-column ForeignKey
    supplier_id: Mapped[uuid.UUID] = mapped_column()
    # {section_key: {"provenance": SectionProvenance, "text": str, "data": ...}}
    # - every SPEC content item, always present (module docstring).
    sections: Mapped[dict[str, Any]] = mapped_column(JSONB(), default=dict)
    # CALCULATED, formula disclosed in sections["price_target"]/["stretch_target"]/
    # ["walk_away_threshold"]["text"]; null when no scored alternative supplier
    # exists to benchmark against (see brief_service.py).
    price_target: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    stretch_target: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    walk_away_threshold: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), default=None)
    draft_email_subject: Mapped[str] = mapped_column(Text())
    draft_email_body: Mapped[str] = mapped_column(Text())
    # app.core.config.NarrativeProviderKind value ("template"|"anthropic").
    narrative_provider: Mapped[str] = mapped_column(Text())
    # ERD-literal name; = NOT AiNarrativeProvider.is_generated (module docstring).
    simulated: Mapped[bool] = mapped_column(Boolean(), default=True)
    state: Mapped[BriefState] = mapped_column(BRIEF_STATE_ENUM, default=BriefState.DRAFT)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(default=None)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))


# UOW insert-ordering relationships (see identity.py comment). organization_id
# participates in two composite FKs here alongside the plain org FK, so
# `foreign_keys` disambiguates which constraint each relationship follows,
# and `overlaps` silences SQLAlchemy's shared-column warning - the same
# pattern as scenarios.py/quotes.py/analysis.py.
NegotiationBrief.organization = relationship(
    "Organization", foreign_keys=[NegotiationBrief.organization_id], lazy="select"
)
NegotiationBrief.scenario = relationship(
    "ComparisonScenario",
    foreign_keys=[NegotiationBrief.organization_id, NegotiationBrief.scenario_id],
    lazy="select",
    overlaps="organization",
)
NegotiationBrief.supplier = relationship(
    "Supplier",
    foreign_keys=[NegotiationBrief.organization_id, NegotiationBrief.supplier_id],
    lazy="select",
    overlaps="organization,scenario",
)
NegotiationBrief.created_by = relationship(
    "User", foreign_keys=[NegotiationBrief.created_by_id], lazy="select"
)
NegotiationBrief.reviewed_by = relationship(
    "User", foreign_keys=[NegotiationBrief.reviewed_by_id], lazy="select"
)


__all__ = [
    "BRIEF_STATE_ENUM",
    "BriefState",
    "NegotiationBrief",
    "SectionProvenance",
]
