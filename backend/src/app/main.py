"""Application factory — PRINCIPAL-OWNED."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.api.middleware import (
    OriginCheckMiddleware,
    RateLimitMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.v1.auth import router as auth_router
from app.api.v1.boms import router as boms_router
from app.api.v1.fx import router as fx_router
from app.api.v1.part_imports import router as part_imports_router
from app.api.v1.parts import router as parts_router
from app.api.v1.rfqs import router as rfqs_router
from app.api.v1.supplier_contacts import router as supplier_contacts_router
from app.api.v1.supplier_performance import router as supplier_performance_router
from app.api.v1.suppliers import router as suppliers_router
from app.api.v1.units import router as units_router
from app.core.clock import Clock, SystemClock
from app.core.config import Environment, Settings, load_settings
from app.core.errors import AppError, ErrorDetail, SafeInternalError, ValidationAppError
from app.core.ids import IdGenerator, RandomIdGenerator
from app.core.logging import configure_logging
from app.db import build_engine, build_session_factory

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    clock = clock or SystemClock()
    ids = ids or RandomIdGenerator()
    configure_logging("INFO" if settings.environment is not Environment.TEST else "WARNING")

    expose_docs = settings.environment is not Environment.PROD or settings.demo_mode
    app = FastAPI(
        title="Procurement Optimizer API",
        version="0.1.0",
        docs_url="/api/docs" if expose_docs else None,
        openapi_url="/api/openapi.json" if expose_docs else None,
    )
    app.state.settings = settings
    app.state.clock = clock
    app.state.ids = ids
    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)

    # middleware: last added runs first. Target order (outermost -> innermost):
    # CORS -> RequestId -> SecurityHeaders -> OriginCheck -> RateLimit -> routes
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(OriginCheckMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, environment=settings.environment)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "If-Match"],
        expose_headers=["X-Request-ID"],
    )

    def _now_iso(request: Request) -> str:
        return clock.now().isoformat()

    def _request_id(request: Request) -> str:
        return str(getattr(request.state, "request_id", ""))

    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=exc.envelope(_request_id(request), _now_iso(request)),
        )

    @app.exception_handler(RequestValidationError)
    def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            ErrorDetail(
                field=".".join(str(p) for p in err.get("loc", []) if p != "body"),
                issue=str(err.get("msg", "invalid")),
            )
            for err in exc.errors()[:20]
        ]
        wrapped = ValidationAppError("Request failed validation.", details=details)
        return JSONResponse(
            status_code=wrapped.status,
            content=wrapped.envelope(_request_id(request), _now_iso(request)),
        )

    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        safe = SafeInternalError(
            request_id=_request_id(request), timestamp=_now_iso(request)
        )
        return JSONResponse(status_code=500, content=safe.envelope())

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(units_router, prefix=API_PREFIX)
    app.include_router(suppliers_router, prefix=API_PREFIX)
    app.include_router(supplier_contacts_router, prefix=API_PREFIX)
    app.include_router(supplier_performance_router, prefix=API_PREFIX)
    app.include_router(parts_router, prefix=API_PREFIX)
    app.include_router(part_imports_router, prefix=API_PREFIX)
    app.include_router(boms_router, prefix=API_PREFIX)
    app.include_router(fx_router, prefix=API_PREFIX)
    app.include_router(rfqs_router, prefix=API_PREFIX)

    return app
