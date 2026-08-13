"""FX service - currency normalization lookups and manual overrides
(SPEC §9, docs/planning/05-calculation-methodology.md §4,
docs/planning/03-api-contract.md §4.13).

Services never commit: the request-scoped `get_db` dependency (app/api/deps.py)
commits on success and rolls back on any raised exception. Every mutation
writes exactly one audit event in the same transaction as the data change it
describes.

**Only manual-override rows are ever persisted here in v0.1** (see
`app.models.fx.ExchangeRate` module docstring) - there is no config-selected
live provider to refresh from (`app.core.config` deliberately has no
`FxRateProviderKind`; `SyntheticFxProvider` is wired explicitly, in code).
`get_effective_rate`'s selection order therefore collapses to:

1. The latest stored row (an org's own manual override) with
   `effective_date <= as_of` for the exact pair - ties on `effective_date`
   are broken in favour of `is_manual_override` (05-calculation-methodology.md
   §4: "manual overrides win over provider rows on the same date"); this
   tie-break can never actually fire in v0.1 since every stored row already
   has `is_manual_override=True`, but it keeps the query correct for a future
   provider-refresh job that persists non-override rows.
2. Otherwise, `SyntheticFxProvider` computed fresh and returned transparently
   - labelled with its own `source` ("synthetic_fixture"), **never persisted**
   (05-calculation-methodology.md §4 never asks a fixture answer to become a
   row of recorded history).
3. If neither yields an answer (unknown currency pair): the calculation
   methodology names this `MissingExchangeRateError` and forbids falling back
   to 1.0. `app.core.errors` is principal-owned and this task may not extend
   it with a new error code, so it is raised as the existing, closest-fit
   `NotFoundError` (404 `not_found` - "no rate exists for this pair" reads
   naturally as "resource absent").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as SaSession

from app.core.clock import Clock
from app.core.errors import ConflictDuplicateError, NotFoundError, ValidationAppError
from app.core.ids import IdGenerator
from app.core.money import quantize_rate
from app.models.fx import ExchangeRate
from app.providers.fx.base import FxRateProvider
from app.providers.fx.synthetic import SyntheticFxProvider
from app.repositories.base import OrgScopedRepository
from app.services.audit import AuditRecorder

MANUAL_SOURCE = "manual"


class _ExchangeRateRepository(OrgScopedRepository[ExchangeRate]):
    model = ExchangeRate

    def latest_on_or_before(self, base: str, quote: str, as_of: date) -> ExchangeRate | None:
        stmt = (
            self._base_query()
            .where(
                ExchangeRate.base_currency == base,
                ExchangeRate.quote_currency == quote,
                ExchangeRate.effective_date <= as_of,
            )
            .order_by(
                ExchangeRate.effective_date.desc(),
                ExchangeRate.is_manual_override.desc(),
            )
            .limit(1)
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def search(
        self,
        *,
        base: str | None,
        quote: str | None,
        as_of: date | None,
        limit: int,
        offset: int,
    ) -> list[ExchangeRate]:
        clauses = self._filters(base=base, quote=quote, as_of=as_of)
        return self.list_page(
            *clauses,
            order_by=ExchangeRate.effective_date.desc(),
            limit=limit,
            offset=offset,
        )

    def count_matching(self, *, base: str | None, quote: str | None, as_of: date | None) -> int:
        clauses = self._filters(base=base, quote=quote, as_of=as_of)
        return self.count(*clauses)

    def _filters(
        self, *, base: str | None, quote: str | None, as_of: date | None
    ) -> list[Any]:
        clauses: list[Any] = []
        if base:
            clauses.append(ExchangeRate.base_currency == base)
        if quote:
            clauses.append(ExchangeRate.quote_currency == quote)
        if as_of is not None:
            clauses.append(ExchangeRate.effective_date <= as_of)
        return clauses


@dataclass(frozen=True, slots=True)
class EffectiveRate:
    """Result of `FxService.get_effective_rate`: the resolved rate plus enough
    provenance to render §4.13's "effective row plus its provenance"."""

    base_currency: str
    quote_currency: str
    as_of: date
    rate: Decimal
    source: str
    is_manual_override: bool
    override_reason: str | None
    effective_date: date
    exchange_rate_id: uuid.UUID | None  # None when served from the unpersisted fixture


def _state(row: ExchangeRate) -> dict[str, Any]:
    """JSON-safe snapshot for audit before/after state."""
    return {
        "base_currency": row.base_currency,
        "quote_currency": row.quote_currency,
        "rate": str(row.rate),
        "effective_date": row.effective_date.isoformat(),
        "source": row.source,
        "is_manual_override": row.is_manual_override,
        "override_reason": row.override_reason,
    }


def _effective_state(effective: EffectiveRate) -> dict[str, Any]:
    return {
        "rate": str(effective.rate),
        "source": effective.source,
        "is_manual_override": effective.is_manual_override,
        "override_reason": effective.override_reason,
        "effective_date": effective.effective_date.isoformat(),
        "exchange_rate_id": (
            str(effective.exchange_rate_id) if effective.exchange_rate_id is not None else None
        ),
    }


class FxService:
    def __init__(
        self,
        session: SaSession,
        organization_id: uuid.UUID,
        audit: AuditRecorder,
        clock: Clock,
        ids: IdGenerator,
        *,
        provider: FxRateProvider | None = None,
    ) -> None:
        self._db = session
        self._organization_id = organization_id
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._provider = provider or SyntheticFxProvider()
        self._repo = _ExchangeRateRepository(session, organization_id)

    def get_effective_rate(self, base: str, quote: str, as_of: date) -> EffectiveRate:
        stored = self._repo.latest_on_or_before(base, quote, as_of)
        if stored is not None:
            return EffectiveRate(
                base_currency=base,
                quote_currency=quote,
                as_of=as_of,
                rate=stored.rate,
                source=stored.source,
                is_manual_override=stored.is_manual_override,
                override_reason=stored.override_reason,
                effective_date=stored.effective_date,
                exchange_rate_id=stored.id,
            )

        rate = self._provider.get_rate(base, quote, as_of)
        if rate is None:
            raise NotFoundError(f"Exchange rate for {base}/{quote}")

        return EffectiveRate(
            base_currency=base,
            quote_currency=quote,
            as_of=as_of,
            rate=rate,
            source=self._provider.describe_source(),
            is_manual_override=False,
            override_reason=None,
            effective_date=as_of,
            exchange_rate_id=None,
        )

    def set_override(
        self,
        *,
        base: str,
        quote: str,
        rate: Decimal,
        effective_date: date,
        reason: str,
        actor_id: uuid.UUID,
    ) -> ExchangeRate:
        if base == quote:
            raise ValidationAppError("base_currency and quote_currency must differ.")

        # Best-effort "what the system would have answered before this override
        # existed" for the audit before-state. A missing rate for a brand-new
        # pair is a legitimate starting point (this override may be the first
        # rate ever recorded for it), not a reason to refuse the write.
        try:
            before = _effective_state(self.get_effective_rate(base, quote, effective_date))
        except NotFoundError:
            before = None

        now = self._clock.now()
        row = ExchangeRate(
            id=self._ids.new_id(),
            organization_id=self._organization_id,
            base_currency=base,
            quote_currency=quote,
            rate=quantize_rate(rate),
            effective_date=effective_date,
            source=MANUAL_SOURCE,
            is_manual_override=True,
            override_reason=reason,
            created_by_id=actor_id,
            retrieved_at=now,
            created_at=now,
        )
        self._repo.add(row)
        try:
            self._db.flush()
        except IntegrityError as exc:
            raise ConflictDuplicateError(
                f"An exchange rate for {base}/{quote} effective {effective_date.isoformat()}"
                " already exists in this organization."
            ) from exc

        self._audit.record(
            "fx.override_set",
            entity_type="exchange_rate",
            entity_id=row.id,
            before_state=before,
            after_state=_state(row),
            explanation=(
                f"Manual FX override set: 1 {base} = {row.rate} {quote}"
                f" effective {effective_date.isoformat()}: {reason}"
            ),
        )
        return row

    def list_rates(
        self,
        *,
        base: str | None = None,
        quote: str | None = None,
        as_of: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ExchangeRate], int]:
        items = self._repo.search(base=base, quote=quote, as_of=as_of, limit=limit, offset=offset)
        total = self._repo.count_matching(base=base, quote=quote, as_of=as_of)
        return items, total

    def refresh(self) -> tuple[str, bool]:
        """`POST /exchange-rates/refresh` (§4.13): "pulls from the configured
        provider; in demo/fixture mode returns `{source:"fixture",
        simulated:true}` and never touches the network." There is no
        config-selected live provider in v0.1 (see module docstring), so this
        is always fixture/simulated mode - no rows are written, matching the
        contract's literal "never touches the network"."""
        return self._provider.describe_source(), True


__all__ = ["MANUAL_SOURCE", "EffectiveRate", "FxService"]
