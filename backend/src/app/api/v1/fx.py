"""Exchange rate routes (docs/planning/03-api-contract.md §4.13). Sync `def`
by policy.

`GET /exchange-rates` serves two shapes from one route, driven by the
contract's own prose: "filters `base`, `quote`, `as_of` (returns the
effective row plus its provenance)". When **all three** of `base`, `quote`,
`as_of` are given, the query is answered as a single *effective-rate lookup*
(`EffectiveExchangeRateResponse`) - manual override wins, else the fixture,
per `FxService.get_effective_rate`. Any other combination (e.g. `base` and
`quote` alone, to see an org's override history for one pair) is a plain
paginated listing of this org's stored override rows
(`ExchangeRateListResponse`), matching every other list endpoint in this API
- it cannot also mean "effective lookup with `as_of` defaulted to today",
because a plain filtered list is a real, distinct need (an audit/history
view) that requiring all three params keeps unambiguous. The contract has no
separate `/exchange-rates/effective` path, so the spec (which
proposed one) yields to the contract here, per the "contract wins"
instruction.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuditDep, ClockDep, DbDep, IdsDep, Principal, PrincipalDep, require_role
from app.models.identity import Role
from app.schemas.fx import (
    EffectiveExchangeRateResponse,
    ExchangeRateListResponse,
    ExchangeRateOverrideCreate,
    ExchangeRateRefreshResponse,
    ExchangeRateResponse,
)
from app.schemas.suppliers import PageInfo
from app.services.fx_service import FxService

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])

# Distinct Query() instances per parameter: FastAPI/Pydantic FieldInfo objects
# carry per-field state, so sharing one instance across two parameters (e.g.
# `base` and `quote` both defaulting to the same object) silently aliases
# them together at request-parsing time. Each parameter needs its own.
_BASE_QUERY = Query(default=None, pattern=r"^[A-Z]{3}$")
_QUOTE_QUERY = Query(default=None, pattern=r"^[A-Z]{3}$")
_AS_OF_QUERY = Query(default=None)


def get_fx_service(
    db: DbDep, principal: PrincipalDep, audit: AuditDep, clock: ClockDep, ids: IdsDep
) -> FxService:
    return FxService(db, principal.organization_id, audit, clock, ids)


FxServiceDep = Annotated[FxService, Depends(get_fx_service)]


@router.get("")
def list_or_get_effective_exchange_rate(
    service: FxServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    base: str | None = _BASE_QUERY,
    quote: str | None = _QUOTE_QUERY,
    as_of: date | None = _AS_OF_QUERY,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ExchangeRateListResponse | EffectiveExchangeRateResponse:
    if base is not None and quote is not None and as_of is not None:
        effective = service.get_effective_rate(base, quote, as_of)
        return EffectiveExchangeRateResponse(
            base_currency=effective.base_currency,
            quote_currency=effective.quote_currency,
            as_of=effective.as_of,
            rate=effective.rate,
            source=effective.source,
            is_manual_override=effective.is_manual_override,
            override_reason=effective.override_reason,
            effective_date=effective.effective_date,
            exchange_rate_id=(
                str(effective.exchange_rate_id)
                if effective.exchange_rate_id is not None
                else None
            ),
        )

    items, total = service.list_rates(
        base=base, quote=quote, as_of=as_of, limit=limit, offset=offset
    )
    return ExchangeRateListResponse(
        items=[ExchangeRateResponse.from_model(r) for r in items],
        page=PageInfo(limit=limit, offset=offset, total=total),
    )


@router.post("", status_code=201)
def create_exchange_rate_override(
    body: ExchangeRateOverrideCreate,
    service: FxServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ExchangeRateResponse:
    row = service.set_override(
        base=body.base_currency,
        quote=body.quote_currency,
        rate=body.rate,
        effective_date=body.effective_date,
        reason=body.override_reason,
        actor_id=principal.user.id,
    )
    return ExchangeRateResponse.from_model(row)


@router.post("/refresh")
def refresh_exchange_rates(
    service: FxServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.ADMINISTRATOR))],
) -> ExchangeRateRefreshResponse:
    source, simulated = service.refresh()
    return ExchangeRateRefreshResponse(source=source, simulated=simulated)


__all__ = ["router"]
