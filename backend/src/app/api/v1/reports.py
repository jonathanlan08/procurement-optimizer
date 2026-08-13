"""Report routes (docs/planning/03-api-contract.md §4.18). Sync `def` by
policy, mirroring every other router in this codebase.

**`POST /reports` runs synchronously and returns `201`, not `202`** - see
`app/services/report_service.py` module docstring "Deviation 1" (same
`JOB_RUNNER=inline` precedent already established by
`api/v1/briefs.py`/`api/v1/scenarios.py`); the response body is the finished
`ReportResponse` with `state` visible (`ready` or `failed`), never a `job`
envelope to poll.

**`GET /reports/{id}/content` returns `410 gone` (hand-built envelope,
`code="not_found"`) when the report has been purged** - see
`report_service.py` module docstring "Deviation 3" for why this is built by
hand here rather than routed through `app.core.errors.AppError` (that
hierarchy has no `410` entry, and `core/errors.py` is off this task's edit
list - "check core/errors.py for the closest code; add none" per the
delegating task's own instruction: `ErrorCode.NOT_FOUND`'s wire value,
`"not_found"`, is that closest code). A report that never produced bytes
(`pending`/`failed`, never purged) is a plain `404 not_found` instead, via
the ordinary `NotFoundError` -> `AppError` exception-handler path in
`app/main.py` (unchanged).

Role: `POST` is `ANALYST`+ (generation is a compute/write action); every
`GET` is `VIEWER`+ - §4.18's own route table ("O A N V" on every read row,
"O A N" only on `POST`) is this contract's explicit, literal answer to its
own §6 gap #1 ("should viewer be able to download reports?"): "yes" - viewer
downloads, never generates.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
from app.core.errors import ErrorCode
from app.models.identity import Role
from app.providers.storage import build_storage_provider
from app.schemas.reports import ReportCreateRequest, ReportListResponse, ReportResponse
from app.services.report_service import ReportPurgedError, ReportService

router = APIRouter(prefix="/reports", tags=["reports"])


def get_report_service(
    db: DbDep,
    principal: PrincipalDep,
    audit: AuditDep,
    clock: ClockDep,
    ids: IdsDep,
    settings: SettingsDep,
) -> ReportService:
    # Built per request from Settings, not injected via api/deps.py
    # (principal-owned, off limits) - mirrors api/v1/documents.py's own
    # get_document_service (providers/storage/__init__.py module docstring).
    storage = build_storage_provider(settings)
    return ReportService(db, principal.organization_id, audit, clock, ids, storage=storage)


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]


# -- create / list / get -----------------------------------------------


@router.post("", status_code=201)
def generate_report(
    body: ReportCreateRequest,
    service: ReportServiceDep,
    principal: Annotated[Principal, Depends(require_role(Role.ANALYST))],
) -> ReportResponse:
    report = service.generate(
        scenario_id=body.scenario_id,
        report_type=body.report_type,
        report_format=body.format,
        parameters=body.parameters,
        actor_id=principal.user.id,
    )
    return ReportResponse.from_model(report)


@router.get("")
def list_reports(
    service: ReportServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    limit: int = 50,
    offset: int = 0,
) -> ReportListResponse:
    items, total = service.list(limit=limit, offset=offset)
    return ReportListResponse(
        items=[ReportResponse.from_model(r) for r in items],
        page={"limit": limit, "offset": offset, "total": total},
    )


@router.get("/{report_id}")
def get_report(
    report_id: UUID,
    service: ReportServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
) -> ReportResponse:
    return ReportResponse.from_model(service.get(report_id))


# -- content download -----------------------------------------------------


@router.get("/{report_id}/content", response_model=None)
def download_report_content(
    report_id: UUID,
    service: ReportServiceDep,
    _principal: Annotated[Principal, Depends(require_role(Role.VIEWER))],
    request: Request,
    clock: ClockDep,
) -> StreamingResponse | JSONResponse:
    try:
        data, content_type, filename = service.get_content(report_id)
    except ReportPurgedError:
        # module docstring: hand-built 410 envelope, no AppError involved -
        # core/errors.py has no 410 ErrorCode and is off this task's edit
        # list. X-Content-Type-Options: nosniff is already applied globally
        # by SecurityHeadersMiddleware (app/api/middleware.py).
        return JSONResponse(
            status_code=410,
            content={
                "error": {
                    "code": ErrorCode.NOT_FOUND.value,
                    "message": (
                        "This report's content has been purged and is no longer available."
                    ),
                    "status": 410,
                    "details": [],
                    "request_id": str(getattr(request.state, "request_id", "")),
                    "timestamp": clock.now().isoformat(),
                }
            },
        )
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter([data]), media_type=content_type, headers=headers)


__all__ = ["router"]
