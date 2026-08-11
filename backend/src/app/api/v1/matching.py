"""Part-matching routes (docs/planning/03-api-contract.md §4.12). Sync `def`
by policy, mirroring every other router in this codebase.

**Contract's literal paths, not this task's own paraphrase** — per this
task's own instruction ("Contract's literal paths win") and the same
resolution `api/v1/extractions.py` already documents for the identical
class of gap:

| Contract §4.12 (used here)              | Task's OBJECTIVE prose (not used)          |
|---|---|
| `POST /quotes/{id}/match`               | `POST /quotes/{quote_id}/match-candidates` |
| `GET /quotes/{id}/matches`              | `GET /quotes/{quote_id}/match-candidates`  |
| `POST /quote-lines/{id}/match`          | `POST /match-candidates/{id}/confirm`      |
| `DELETE /quote-lines/{id}/match`        | `POST /match-candidates/{id}/reject`       |

The contract's confirm/unmatch routes are addressed by *quote line*, with an
explicit `rfq_line_id` in the request body, not by a `PartMatchCandidate`
id at all — see `services/matching_service.py`'s own module docstring for
how `confirm_line_match`/`unmatch_line` are built around that literal shape.

**Two routers, one module** — same shape `api/v1/extractions.py`/
`api/v1/quotes.py` already establish: `quotes_match_router` (prefix
`/quotes`) owns generate/list, `quote_lines_match_router` (prefix
`/quote-lines`) owns confirm/unmatch. Both are exported and both mounted in
`app/main.py`.

**`POST /quotes/{id}/match` runs synchronously and returns `200`, not `202`
+ a job envelope.** §1.5 documents `202`/job-polling as the general case but
explicitly allows "sync if fast" per this route's own annotation; this
codebase has no job-queue worker at all (`services/extraction_service.py`'s
own module docstring: `JOB_RUNNER=inline` is the only shape this v0.1 build
implements anywhere), so `generate_for_quote` always runs inline, the same
established precedent `start_extraction_run` already sets. `200`, not
`201`, because the response is a freshly-recomputed *view* over
`part_match_candidates` (`QuoteMatchesResponse`, one entry per quote line),
not a single newly-created addressable resource.

**`POST`/`DELETE /quote-lines/{id}/match` return `QuoteLineResponse`**, the
same schema `api/v1/quotes.py` already uses for a quote line — mirroring
`api/v1/rfqs.py`'s `DELETE /rfqs/{id}/suppliers/{id}` (exclude), which
likewise returns the updated resource rather than `204`.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ValidationAppError
from app.models.identity import Role
from app.schemas.matching import (
    QuoteLineMatchConfirmRequest,
    QuoteLineUnmatchRequest,
    QuoteMatchesResponse,
)
from app.schemas.quotes import QuoteLineResponse
from app.services.matching_service import MatchingService

quotes_match_router = APIRouter(prefix="/quotes", tags=["matching"])
quote_lines_match_router = APIRouter(prefix="/quote-lines", tags=["matching"])


def get_matching_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> MatchingService:
    return MatchingService(db, principal.organization_id, audit, clock, ids)


MatchingServiceDep = Annotated[MatchingService, Depends(get_matching_service)]


# -- generate / list, addressed by quote --------------------------------


@quotes_match_router.post("/{quote_id}/match")
def generate_quote_matches(
    quote_id: UUID,
    service: MatchingServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> QuoteMatchesResponse:
    lines_with_candidates = service.generate_for_quote(quote_id, actor_id=principal.user.id)
    return QuoteMatchesResponse.from_models(lines_with_candidates)


@quotes_match_router.get("/{quote_id}/matches")
def list_quote_matches(
    quote_id: UUID,
    service: MatchingServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> QuoteMatchesResponse:
    lines_with_candidates = service.list_for_quote(quote_id)
    return QuoteMatchesResponse.from_models(lines_with_candidates)


# -- confirm / unmatch, addressed by quote line --------------------------


@quote_lines_match_router.post("/{quote_line_id}/match")
def confirm_quote_line_match(
    quote_line_id: UUID,
    body: QuoteLineMatchConfirmRequest,
    service: MatchingServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> QuoteLineResponse:
    if not body.confirmed:
        raise ValidationAppError(
            "This route only confirms a match; set confirmed=true, or use "
            "DELETE /quote-lines/{id}/match to unmatch."
        )
    line = service.confirm_line_match(
        quote_line_id,
        rfq_line_id=body.rfq_line_id,
        reason=body.reason,
        actor_id=principal.user.id,
    )
    price_breaks = service.price_breaks_for_line(line.id)
    return QuoteLineResponse.from_model(line, price_breaks)


@quote_lines_match_router.delete("/{quote_line_id}/match")
def unmatch_quote_line(
    quote_line_id: UUID,
    service: MatchingServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    body: QuoteLineUnmatchRequest | None = None,
) -> QuoteLineResponse:
    reason = body.reason if body is not None else None
    line = service.unmatch_line(quote_line_id, reason=reason, actor_id=principal.user.id)
    price_breaks = service.price_breaks_for_line(line.id)
    return QuoteLineResponse.from_model(line, price_breaks)


__all__ = ["quote_lines_match_router", "quotes_match_router"]
