"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from vms_contracts.version import API_PREFIX

from app.api.internal import media_auth as internal_media_auth
from app.api.v1 import api_router
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.core.errors import VmsError
from app.db import session as db
from app.lifecycle import shutdown, startup
from app.logging_setup import bind_correlation_id, configure_logging, correlation_id, get_logger
from app.version import PRODUCT_NAME, VERSION, version_info

log = get_logger("api")

DESCRIPTION = """
Intelligent VMS control API.

**Release maturity: alpha.** The implementation status of every feature is
published in `docs/requirements/feature-matrix.md`. Endpoints that correspond
to designed-but-not-implemented capabilities return `501 not_implemented` with
an explanation rather than a partial result.
"""


def _problem(status: int, code: str, title: str, detail: str, **extra) -> JSONResponse:
    body = {
        "type": f"https://docs.intelligent-vms.local/errors/{code}",
        "title": title, "status": status, "code": code, "detail": detail,
        "correlation_id": correlation_id.get(),
    }
    body.update(extra)
    return JSONResponse(status_code=status, content=body,
                        media_type="application/problem+json")


def create_app(settings: Settings | None = None,
               container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if application.state.container is None:
            application.state.container = build_container(settings)
        await startup(application.state.container)
        try:
            yield
        finally:
            await shutdown(application.state.container)

    app = FastAPI(
        title=f"{PRODUCT_NAME} API", version=VERSION, description=DESCRIPTION,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs", redoc_url=f"{API_PREFIX}/redoc",
        root_path=settings.api_root_path, lifespan=lifespan)
    app.state.container = container

    if settings.trusted_hosts and settings.trusted_hosts != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
            expose_headers=["X-Correlation-Id"])

    @app.middleware("http")
    async def correlation_and_security(request: Request, call_next):  # noqa: ANN001
        correlation = bind_correlation_id(request.headers.get("x-correlation-id"))
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = correlation
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(VmsError)
    async def vms_error_handler(request: Request, exc: VmsError):  # noqa: ANN001
        if exc.status_code >= 500:
            log.error("request.failed", path=request.url.path, code=exc.code,
                      detail=exc.detail, exc_info=True)
        else:
            log.info("request.rejected", path=request.url.path, code=exc.code,
                     status=exc.status_code)
        return JSONResponse(status_code=exc.status_code,
                            content=exc.to_problem(correlation_id.get()),
                            media_type="application/problem+json")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request,  # noqa: ANN001
                                 exc: RequestValidationError):
        errors = [{"field": ".".join(str(p) for p in e["loc"][1:]),
                   "message": e["msg"]} for e in exc.errors()][:20]
        return _problem(422, "invalid_request", "Request validation failed",
                        "One or more fields are invalid.", errors=errors)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):  # noqa: ANN001
        log.error("request.unhandled", path=request.url.path, error=str(exc),
                  exc_info=True)
        return _problem(500, "internal_error", "Internal error",
                        "An unexpected error occurred. Quote the correlation id "
                        "when contacting support.")

    app.include_router(api_router, prefix=API_PREFIX)
    # Gateway admission hook (ADR-0015). Outside /api/v1, excluded from the
    # OpenAPI contract, and protected by caller authentication -- not by
    # being undocumented.
    app.include_router(internal_media_auth.router)

    @app.get("/healthz", tags=["health"], summary="Liveness probe")
    async def liveness() -> dict:
        return {"status": "alive", "version": VERSION}

    @app.get("/readyz", tags=["health"], summary="Readiness probe")
    async def readiness():
        current = app.state.container
        if current is None:
            return JSONResponse(status_code=503, content={"status": "starting"})
        report = await current.health.check()
        code = 200 if report.status.value in ("healthy", "degraded") else 503
        return JSONResponse(status_code=code, content={
            "status": report.status.value,
            "components": {c.name: c.status.value for c in report.components}})

    @app.get("/", tags=["health"], summary="Service banner")
    async def root() -> dict:
        info = version_info()
        info["api"] = API_PREFIX
        info["documentation"] = f"{API_PREFIX}/docs"
        info["notice"] = ("Alpha build. See docs/requirements/feature-matrix.md "
                          "for the implementation status of every feature.")
        return info

    return app


def run() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = get_settings()
    db.get_engine(settings)
    uvicorn.run("app.main:create_app", factory=True, host=settings.api_host,
                port=settings.api_port, log_config=None, proxy_headers=True,
                # "*" would let any client rewrite its apparent source address
                # with X-Forwarded-For. Trust only configured proxies.
                forwarded_allow_ips=settings.forwarded_allow_ips)


if __name__ == "__main__":  # pragma: no cover
    run()
