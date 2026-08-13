"""Extraction routes (docs/planning/03-api-contract.md §4.10). Sync `def` by
policy, mirroring every other router in this codebase.

**Literal contract paths, not the delegating task's own paraphrase** - per
this task's own instruction ("Follow the contract's literal paths where they
differ") and the same resolution `api/v1/documents.py`/`api/v1/quotes.py`
already document for the identical class of gap:

| Contract (used here)                          | Task's OBJECTIVE prose (not used)    |
|---|---|
| `POST/GET /quote-documents/{id}/extraction-runs` | `POST /quote-documents/{id}/extractions` |
| `GET /extraction-runs/{id}/fields` (filterable)  | *(fields folded into run-detail GET)*    |
| `PATCH /extraction-runs/{id}/fields/{fid}`       | `POST .../confirm` + `POST .../correct`  |
| `POST /extraction-runs/{id}/confirm`             | `POST /extraction-runs/{id}/materialize` |

**Two routers, one module** - same shape `api/v1/documents.py`/
`api/v1/quotes.py` already establish: `document_extractions_router` nests
start/list under the parent document (`/quote-documents/{id}/extraction-runs`,
§4.10's own route table), while every other route addresses the run
directly (`/extraction-runs/{id}...`). Both are exported and both mounted in
`app/main.py`.

**`POST .../extraction-runs` returns `201` with the completed run body, not
`202` + a job envelope.** §1.5 documents this exact shape as the
`JOB_RUNNER=inline` case ("`201` with the completed resource... documented,
so contract tests assert both shapes") - this v0.1 build only ever runs
inline (`services/extraction_service.py`'s own module docstring), so that is
the only shape implemented.

**`PATCH .../fields/{fid}` dispatches to `confirm_field`/`correct_field` from
one combined body**, per §4.10's own literal request shape
(`{normalized_value?, is_confirmed, reason?}`): a present `normalized_value`
is a correction (which the service always confirms as a side effect of
correcting); its absence with `is_confirmed=true` is a plain confirmation;
neither combination is a no-op the route rejects with `422`.

**`POST .../confirm` (materialize) returns a `QuoteResponse`** (the same
schema `api/v1/quotes.py` already uses), including its `ETag` header -
`materialize()` builds a real `Quote`, and every other route in this
codebase that returns one sets the same header (`api/v1/quotes.py`'s own
`_set_etag`, duplicated here as a two-line inline rather than importing a
private helper from another router module).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import (
    AuditDep,
    ClockDep,
    DbDep,
    IdsDep,
    Principal,
    PrincipalDep,
    SettingsDep,
    require_role,
)
from app.core.errors import ValidationAppError
from app.domain.confidence import ConfidenceBand
from app.models.identity import Role
from app.providers.storage import build_storage_provider
from app.schemas.extractions import (
    ExtractionFieldListResponse,
    ExtractionFieldPatchRequest,
    ExtractionFieldResponse,
    ExtractionMaterializeRequest,
    ExtractionRunListResponse,
    ExtractionRunResponse,
)
from app.schemas.quotes import QuoteResponse
from app.services.extraction_service import ExtractionService, build_extraction_provider

document_extractions_router = APIRouter(
    prefix="/quote-documents/{document_id}/extraction-runs", tags=["extractions"]
)
router = APIRouter(prefix="/extraction-runs", tags=["extractions"])


def get_extraction_service(
    db: DbDep,
    principal: PrincipalDep,
    audit: AuditDep,
    clock: ClockDep,
    ids: IdsDep,
    settings: SettingsDep,
) -> ExtractionService:
    # Built per request from Settings, not injected via api/deps.py
    # (principal-owned, off limits) - same pattern api/v1/documents.py's
    # get_document_service already establishes for build_storage_provider.
    storage = build_storage_provider(settings)
    provider = build_extraction_provider(settings)
    return ExtractionService(
        db, principal.organization_id, audit, clock, ids, storage=storage, provider=provider
    )


ExtractionServiceDep = Annotated[ExtractionService, Depends(get_extraction_service)]


# -- nested under the document: start/list -----------------------------


@document_extractions_router.post("", status_code=201)
def start_extraction_run(
    document_id: UUID,
    service: ExtractionServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ExtractionRunResponse:
    run = service.start_run(document_id, actor_id=principal.user.id)
    return ExtractionRunResponse.from_model(run)


@document_extractions_router.get("")
def list_document_extraction_runs(
    document_id: UUID,
    service: ExtractionServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> ExtractionRunListResponse:
    runs = service.list_runs(document_id)
    return ExtractionRunListResponse(items=[ExtractionRunResponse.from_model(r) for r in runs])


# -- addressed directly ------------------------------------------------


@router.get("/{run_id}")
def get_extraction_run(
    run_id: UUID,
    service: ExtractionServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> ExtractionRunResponse:
    run = service.get_run(run_id)
    return ExtractionRunResponse.from_model(run)


@router.get("/{run_id}/fields")
def list_extraction_fields(
    run_id: UUID,
    service: ExtractionServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    # B008 suppressed: Query()/enum defaults are side-effect-free, the same
    # reasoning api/v1/documents.py's list_rfq_documents already documents
    # for its own state/supplier_id filters.
    requires_confirmation: bool | None = Query(default=None),
    band: ConfidenceBand | None = Query(default=None),  # noqa: B008
    is_confirmed: bool | None = Query(default=None),
) -> ExtractionFieldListResponse:
    fields = service.list_fields(
        run_id,
        requires_confirmation=requires_confirmation,
        band=band,
        is_confirmed=is_confirmed,
    )
    items = [ExtractionFieldResponse.from_model(f) for f in fields]
    return ExtractionFieldListResponse(items=items)


@router.patch("/{run_id}/fields/{field_id}")
def patch_extraction_field(
    run_id: UUID,
    field_id: UUID,
    body: ExtractionFieldPatchRequest,
    service: ExtractionServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ExtractionFieldResponse:
    if body.normalized_value is not None:
        field = service.correct_field(
            run_id,
            field_id,
            new_value=body.normalized_value,
            reason=body.reason,
            actor_id=principal.user.id,
        )
    elif body.is_confirmed:
        field = service.confirm_field(run_id, field_id, actor_id=principal.user.id)
    else:
        raise ValidationAppError(
            "This request has no effect: set is_confirmed=true to confirm the "
            "field, or supply normalized_value to correct it."
        )
    return ExtractionFieldResponse.from_model(field)


@router.post("/{run_id}/fields/confirm-all")
def confirm_all_extraction_fields(
    run_id: UUID,
    service: ExtractionServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ExtractionRunResponse:
    """2026-08 product-audit remediation, P2 (bulk field confirmation). No
    request body (yet) - every field still requiring confirmation on this
    run is confirmed in one call, see `ExtractionService.confirm_all_fields`.

    Returns the run detail, not a field (unlike `patch_extraction_field`
    above): this task's brief explicitly allows either "the same shape the
    single-field confirm returns, or the run detail", and a bulk action over
    an unbounded number of fields has no single field to return - the run
    (now `ready`, or unchanged if there was nothing pending) is the
    meaningful result here.
    """
    run = service.confirm_all_fields(run_id, actor_id=principal.user.id)
    return ExtractionRunResponse.from_model(run)


@router.post("/{run_id}/confirm", status_code=201)
def materialize_extraction_run(
    run_id: UUID,
    body: ExtractionMaterializeRequest,
    service: ExtractionServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
    response: Response,
) -> QuoteResponse:
    quote, lines, breaks_by_line, terms = service.materialize(
        run_id, supplier_id=body.supplier_id, actor_id=principal.user.id
    )
    response.headers["ETag"] = f'"{quote.version}"'
    return QuoteResponse.from_model(quote, lines, breaks_by_line, terms)


__all__ = ["document_extractions_router", "router"]
