"""Quote routes — manual entry only (docs/planning/03-api-contract.md
§4.11). Sync `def` by policy, mirroring api/v1/rfqs.py / api/v1/boms.py.

**Two routers, one module.** Every other route module in this codebase
mounts a single `APIRouter` at one prefix; quotes genuinely need two —
`POST`/`GET` create/list are nested under the parent RFQ
(`/rfqs/{rfq_id}/quotes`, §4.11's own route table), while every other quote
route addresses the quote directly (`/quotes/{quote_id}`, ..., per §6's
"sub-resource vs top-level ids" contract-gap note, already resolved in favor
of top-level ids for `extraction-runs`/`quotes` elsewhere in the same
section). `rfq_quotes_router` and `router` are both exported and both
mounted in `app/main.py`, rather than forcing one shape onto both URL
families.

Concurrency (§1.7): quotes carry `ETag`/`If-Match`. Every response that
returns a quote sets `ETag: "<version>"`. Only `PATCH /quotes/{id}` requires
`If-Match` on the way in — `POST .../supersede` and `POST .../archive`
mutate state and return a fresh `ETag` without demanding the header as
input, the same split already established by `api/v1/rfqs.py` for
`POST .../status` vs `PATCH /rfqs/{id}` (see that module's docstring, and
services/quote_service.py's own docstring for why this split is safe here
too).

Deviations from the contract's literal §4.11 route table, contract wins per
instructions but both are recorded here:

1. **`POST /quotes/{id}/supersede`** and **`POST /quotes/{id}/archive`**
   have no entry in §4.11's table at all — the contract's own `PATCH
   /quotes/{id}` row alludes to supersession happening as a *side effect* of
   editing a `confirmed` quote ("edits after confirmed create revision + 1
   and supersede"), and never mentions archiving quotes anywhere. This
   task's brief explicitly asks for both as standalone service methods
   (`QuoteService.supersede`/`.archive`) with their own audit event types
   (`quote.superseded`/`quote.archived`), the same kind of contract-silent,
   brief-mandated addition already made for `POST /boms/{id}/activate` and
   `POST /rfqs/{id}/suppliers/{id}/reinstate` elsewhere in this codebase —
   see those modules' docstrings for the identical judgement call. `archive`
   is gated `O A` (administrator+), mirroring every other archivable
   resource's archive route (suppliers/parts/BOMs); `supersede` is gated
   `O A N` (analyst+), matching `create`.
2. **No `PUT .../price-breaks`, `PUT .../terms`, `GET/POST/PATCH/DELETE
   .../lines[...]`, `GET .../corrections`, `POST .../confirm`, or any
   `/quotes/{id}/match(es)` route (§4.12).** All out of scope for this
   task's manual-entry brief — see services/quote_service.py's module
   docstring "Scope: manual entry only".
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.core.errors import ValidationAppError
from app.models.identity import Role
from app.models.quotes import Quote, QuoteLine, QuotePriceBreak, QuoteTerms
from app.schemas.quotes import (
    QuoteArchiveRequest,
    QuoteCreate,
    QuoteListResponse,
    QuoteResponse,
    QuoteSummaryResponse,
    QuoteSupersedeRequest,
    QuoteUpdate,
)
from app.services.quote_service import QuoteService

rfq_quotes_router = APIRouter(prefix="/rfqs/{rfq_id}/quotes", tags=["quotes"])
router = APIRouter(prefix="/quotes", tags=["quotes"])


def get_quote_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> QuoteService:
    return QuoteService(db, principal.organization_id, audit, clock, ids)


QuoteServiceDep = Annotated[QuoteService, Depends(get_quote_service)]


def _parse_if_match(if_match: str) -> int:
    raw = if_match.strip().strip('"')
    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationAppError(
            "If-Match must be the resource's current integer version, e.g. \"3\"."
        ) from exc


def _set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'


def _quote_response(
    quote: Quote,
    lines: list[QuoteLine],
    breaks_by_line: dict[UUID, list[QuotePriceBreak]],
    terms: QuoteTerms | None,
    response: Response,
) -> QuoteResponse:
    _set_etag(response, quote.version)
    return QuoteResponse.from_model(quote, lines, breaks_by_line, terms)


# -- nested under the RFQ: list/create --------------------------------------


@rfq_quotes_router.get("")
def list_rfq_quotes(
    rfq_id: UUID,
    service: QuoteServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    include_superseded: bool = Query(default=False),
) -> QuoteListResponse:
    quotes = service.list_by_rfq(rfq_id, include_superseded=include_superseded)
    return QuoteListResponse(
        items=[
            QuoteSummaryResponse.from_model(q, line_count=service.line_count(q.id))
            for q in quotes
        ]
    )


@rfq_quotes_router.post("", status_code=201)
def create_rfq_quote(
    rfq_id: UUID,
    body: QuoteCreate,
    service: QuoteServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> QuoteResponse:
    quote, lines, breaks_by_line, terms = service.create(rfq_id, body)
    return _quote_response(quote, lines, breaks_by_line, terms, response)


# -- addressed directly ------------------------------------------------


@router.get("/{quote_id}")
def get_quote(
    quote_id: UUID,
    service: QuoteServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    response: Response,
) -> QuoteResponse:
    quote, lines, breaks_by_line, terms = service.get(quote_id)
    return _quote_response(quote, lines, breaks_by_line, terms, response)


@router.patch("/{quote_id}")
def update_quote(
    quote_id: UUID,
    body: QuoteUpdate,
    service: QuoteServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match")],
) -> QuoteResponse:
    version = _parse_if_match(if_match)
    quote, lines, breaks_by_line, terms = service.update(
        quote_id, body, if_match_version=version
    )
    return _quote_response(quote, lines, breaks_by_line, terms, response)


@router.post("/{quote_id}/supersede", status_code=201)
def supersede_quote(
    quote_id: UUID,
    body: QuoteSupersedeRequest,
    service: QuoteServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> QuoteResponse:
    quote, lines, breaks_by_line, terms = service.supersede(quote_id, body)
    return _quote_response(quote, lines, breaks_by_line, terms, response)


@router.post("/{quote_id}/archive")
def archive_quote(
    quote_id: UUID,
    body: QuoteArchiveRequest,
    service: QuoteServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
    response: Response,
) -> QuoteResponse:
    quote, lines, breaks_by_line, terms = service.archive(
        quote_id, reason=body.reason, actor_id=principal.user.id
    )
    return _quote_response(quote, lines, breaks_by_line, terms, response)


__all__ = ["rfq_quotes_router", "router"]
