from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.security_posture import validate_security_posture
from app.db.base import Base
from app.db.session import engine
from app.routers import ai, alarms, auth_session, cameras, diagnostics, events, health, live_media, manual_recordings, onvif, placement, recordings, system
from app.observability import PrometheusMiddleware, router as observability_router
from app.security_audit import SecurityAuditMiddleware
from app.services.events import event_publisher
from app.services.health_monitor import health_monitor
from app.services.outbox import outbox_worker
from app.services.reconciler import reconciler_loop


def _cors_allowlist(value: str, setting_name: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if "*" in items:
        raise ValueError(f"{setting_name} must list explicit values; wildcard is not allowed")
    return items


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop control-plane background services with the API lifecycle.

    Args:
        app: FastAPI application instance managing this lifecycle.

    Yields:
        Control to FastAPI while background services are running.

    Raises:
        Exception: Propagates startup/shutdown failures so the service does not
            report a healthy lifecycle after a failed dependency transition.
    """
    validate_security_posture()

    if settings.auto_create_schema:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    await event_publisher.start()
    await outbox_worker.start()
    await reconciler_loop.start()
    await health_monitor.start()
    try:
        yield
    finally:
        await health_monitor.stop()
        await reconciler_loop.stop()
        await outbox_worker.stop()
        await event_publisher.stop()
        await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.7.0", lifespan=lifespan)
install_error_handlers(app)
app.add_middleware(PrometheusMiddleware)
app.add_middleware(SecurityAuditMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowlist(settings.cors_allowed_origins, "CORS_ALLOWED_ORIGINS"),
    allow_credentials=False,
    allow_methods=_cors_allowlist(settings.cors_allowed_methods, "CORS_ALLOWED_METHODS"),
    allow_headers=_cors_allowlist(settings.cors_allowed_headers, "CORS_ALLOWED_HEADERS"),
)
app.include_router(observability_router)
app.include_router(system.router)
app.include_router(auth_session.router)
app.include_router(cameras.router)
app.include_router(cameras.group_router)
app.include_router(live_media.router)
app.include_router(live_media.internal_router)
app.include_router(onvif.router)
app.include_router(recordings.router)
app.include_router(manual_recordings.router)
app.include_router(recordings.internal_router)
app.include_router(events.router)
app.include_router(events.internal_router)
app.include_router(health.router)
app.include_router(alarms.router)
app.include_router(diagnostics.router)

app.include_router(ai.router)
app.include_router(placement.router)
