"""Negotiation-brief request/response schemas (docs/planning/
03-api-contract.md §4.17, app/models/briefs.py, app/services/brief_service.py).

Wire conventions (§1.2): decimals are JSON strings, never numbers -
`UnitPriceString` (reused from `app.schemas.parts`, `NUMERIC(18,8)`/8dp,
matching `price_target`/`stretch_target`/`walk_away_threshold`'s column
scale) is used for both the optional request-side overrides and the
response figures.

`NegotiationBriefResponse.requires_review` is derived (`state ==
BriefState.DRAFT`), not a stored column - this task's own OBJECTIVE
explicitly asks the `GET` response to "prominently include review state and
narrative_is_generated"; `requires_review` makes "is this safe to act on
yet" a single boolean a client can check without knowing the state enum's
member spelling, alongside the literal `state` string. `narrative_is_generated`
is the task's own paraphrase name for `AiNarrativeProvider.is_generated`,
exposed here as the literal negation of the stored `simulated` column (see
`app/models/briefs.py` module docstring for why the DB column itself is
named - and stores - the opposite polarity, ERD-literal `simulated`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.briefs import NegotiationBrief
from app.schemas.parts import UnitPriceString

# -- requests ----------------------------------------------------------


class BriefTargetsRequest(BaseModel):
    """Optional human overrides for the CALCULATED targets (03-api-contract.md
    §4.17's `targets?` request field) - each supplied value is stored with
    `SectionProvenance.USER_ASSUMPTION`, never silently relabelled
    CALCULATED (app/services/brief_service.py `generate`)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    price_target: UnitPriceString | None = Field(default=None, ge=0)
    stretch_target: UnitPriceString | None = Field(default=None, ge=0)
    walk_away_threshold: UnitPriceString | None = Field(default=None, ge=0)


class BriefGenerateRequest(BaseModel):
    """`POST /comparison-scenarios/{id}/negotiation-briefs` body
    (03-api-contract.md §4.17: `{supplier_id, objective, targets?}`)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    supplier_id: uuid.UUID
    objective: str | None = Field(default=None, max_length=4000)
    targets: BriefTargetsRequest | None = None


class BriefReviewRequest(BaseModel):
    """`POST /negotiation-briefs/{id}/review` body (03-api-contract.md
    §4.17: `{approved:true, reviewer_notes}` -> `state=human_reviewed`)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    approved: bool = True
    reviewer_notes: str | None = Field(default=None, max_length=4000)


class BriefArchiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)


# -- responses -----------------------------------------------------------


class BriefSectionResponse(BaseModel):
    """One SPEC content item: `provenance ∈ supplier_provided|user_assumption|
    calculated|ai_narrative|missing` (03-api-contract.md §4.17's own literal
    wording), the rendered `text`, and an optional structured `data` payload."""

    provenance: str
    text: str
    data: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> BriefSectionResponse:
        return cls(provenance=raw["provenance"], text=raw["text"], data=raw.get("data"))


class NegotiationBriefResponse(BaseModel):
    """`GET /negotiation-briefs/{id}` - every SPEC content item under
    `sections`, each carrying its own `provenance`; review state and
    `narrative_is_generated`/`simulated` prominent at the top level (module
    docstring)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    scenario_id: str
    supplier_id: str
    sections: dict[str, BriefSectionResponse]
    price_target: UnitPriceString | None
    stretch_target: UnitPriceString | None
    walk_away_threshold: UnitPriceString | None
    draft_email_subject: str
    draft_email_body: str
    narrative_provider: str
    simulated: bool
    narrative_is_generated: bool
    state: str
    requires_review: bool
    reviewed_by_id: str | None
    # resolved display name (org-scoped); None when the reviewer is unknown -
    # a raw reviewer UUID on a reviewed brief was a 2026-08 external-review P3
    reviewed_by_full_name: str | None = None
    reviewed_at: datetime | None
    created_by_id: str
    created_at: datetime
    updated_at: datetime
    version: int
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None

    @classmethod
    def from_model(
        cls, brief: NegotiationBrief, *, reviewed_by_full_name: str | None = None
    ) -> NegotiationBriefResponse:
        return cls(
            id=str(brief.id),
            scenario_id=str(brief.scenario_id),
            supplier_id=str(brief.supplier_id),
            sections={
                key: BriefSectionResponse.from_json(value) for key, value in brief.sections.items()
            },
            price_target=brief.price_target,
            stretch_target=brief.stretch_target,
            walk_away_threshold=brief.walk_away_threshold,
            draft_email_subject=brief.draft_email_subject,
            draft_email_body=brief.draft_email_body,
            narrative_provider=brief.narrative_provider,
            simulated=brief.simulated,
            narrative_is_generated=not brief.simulated,
            state=brief.state.value,
            requires_review=(brief.state.value == "draft"),
            reviewed_by_id=(
                str(brief.reviewed_by_id) if brief.reviewed_by_id is not None else None
            ),
            reviewed_by_full_name=reviewed_by_full_name,
            reviewed_at=brief.reviewed_at,
            created_by_id=str(brief.created_by_id),
            created_at=brief.created_at,
            updated_at=brief.updated_at,
            version=brief.version,
            is_archived=brief.is_archived,
            archived_at=brief.archived_at,
            archive_reason=brief.archive_reason,
        )


class NegotiationBriefSummaryResponse(BaseModel):
    """List views (`GET /comparison-scenarios/{id}/negotiation-briefs`,
    `GET /rfqs/{id}/negotiation-briefs`) - lean, no embedded `sections`."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    scenario_id: str
    supplier_id: str
    state: str
    requires_review: bool
    narrative_provider: str
    simulated: bool
    created_by_id: str
    created_at: datetime
    is_archived: bool

    @classmethod
    def from_model(cls, brief: NegotiationBrief) -> NegotiationBriefSummaryResponse:
        return cls(
            id=str(brief.id),
            scenario_id=str(brief.scenario_id),
            supplier_id=str(brief.supplier_id),
            state=brief.state.value,
            requires_review=(brief.state.value == "draft"),
            narrative_provider=brief.narrative_provider,
            simulated=brief.simulated,
            created_by_id=str(brief.created_by_id),
            created_at=brief.created_at,
            is_archived=brief.is_archived,
        )


class NegotiationBriefListResponse(BaseModel):
    items: list[NegotiationBriefSummaryResponse]
    page: dict[str, Any]


__all__ = [
    "BriefArchiveRequest",
    "BriefGenerateRequest",
    "BriefReviewRequest",
    "BriefSectionResponse",
    "BriefTargetsRequest",
    "NegotiationBriefListResponse",
    "NegotiationBriefResponse",
    "NegotiationBriefSummaryResponse",
]
