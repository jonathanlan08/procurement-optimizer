"""Part-matching request/response schemas (docs/planning/03-api-contract.md
§4.12, app/models/documents.py's `PartMatchCandidate`).

Not on the delegating task's own "NEW files" list — the same
not-listed-but-necessary situation `app/schemas/extractions.py`'s own module
docstring already documents for an earlier phase.

**`GET /quotes/{id}/matches`'s own literal annotation** ("per quote line:
ranked candidates with `strategy`, `confidence`, `explanation`") shapes
`QuoteMatchesResponse` as one entry per quote line, each carrying its own
ranked candidate list — not a flat candidate list — matching that sentence
exactly. `POST /quotes/{id}/match` (generate) returns the same shape, since
it is "the same computation, freshly run" rather than a different resource.

Money/decimal fields are JSON strings (§1.2): `confidence`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.documents import PartMatchCandidate
from app.models.quotes import QuoteLine

# -- candidates --------------------------------------------------------


class MatchCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rfq_line_id: str
    part_id: str
    strategy: str
    confidence: str
    explanation: str | None
    rank: int
    is_selected: bool
    human_confirmed: bool
    confirmed_by_id: str | None
    confirmed_at: datetime | None

    @classmethod
    def from_model(cls, candidate: PartMatchCandidate) -> MatchCandidateResponse:
        return cls(
            id=str(candidate.id),
            rfq_line_id=str(candidate.rfq_line_id),
            part_id=str(candidate.part_id),
            strategy=candidate.strategy.value,
            confidence=str(candidate.confidence),
            explanation=candidate.explanation,
            rank=candidate.rank,
            is_selected=candidate.is_selected,
            human_confirmed=candidate.human_confirmed,
            confirmed_by_id=(
                str(candidate.confirmed_by_id) if candidate.confirmed_by_id is not None else None
            ),
            confirmed_at=candidate.confirmed_at,
        )


class QuoteLineMatchesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    quote_line_id: str
    match_status: str
    matched_rfq_line_id: str | None
    part_id: str | None
    candidates: list[MatchCandidateResponse]

    @classmethod
    def from_model(
        cls, line: QuoteLine, candidates: list[PartMatchCandidate]
    ) -> QuoteLineMatchesResponse:
        return cls(
            quote_line_id=str(line.id),
            match_status=line.match_status.value,
            matched_rfq_line_id=(
                str(line.matched_rfq_line_id) if line.matched_rfq_line_id is not None else None
            ),
            part_id=(str(line.part_id) if line.part_id is not None else None),
            candidates=[MatchCandidateResponse.from_model(c) for c in candidates],
        )


class QuoteMatchesResponse(BaseModel):
    items: list[QuoteLineMatchesResponse]

    @classmethod
    def from_models(
        cls, lines_with_candidates: list[tuple[QuoteLine, list[PartMatchCandidate]]]
    ) -> QuoteMatchesResponse:
        return cls(
            items=[
                QuoteLineMatchesResponse.from_model(line, candidates)
                for line, candidates in lines_with_candidates
            ]
        )


# -- confirm / unmatch -------------------------------------------------


class QuoteLineMatchConfirmRequest(BaseModel):
    """`POST /quote-lines/{id}/match` body, §4.12's own literal shape
    (`{rfq_line_id, confirmed:true, reason?}`). `confirmed` has no `?` in the
    contract's own annotation (unlike `reason`) — required, and validated
    `True` (this route only ever confirms; `DELETE .../match` is the
    unmatch path)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    rfq_line_id: uuid.UUID
    confirmed: bool
    reason: str | None = Field(default=None, max_length=1000)


class QuoteLineUnmatchRequest(BaseModel):
    """`DELETE /quote-lines/{id}/match` body. §4.12's own terse annotation
    ("unmatch, audited") does not spell out a body shape; `reason` is
    optional, mirroring `ExtractionFieldPatchRequest.reason`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


__all__ = [
    "MatchCandidateResponse",
    "QuoteLineMatchConfirmRequest",
    "QuoteLineMatchesResponse",
    "QuoteLineUnmatchRequest",
    "QuoteMatchesResponse",
]
