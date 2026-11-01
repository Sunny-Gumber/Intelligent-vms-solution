"""API v1 router assembly."""

from fastapi import APIRouter

from app.api.v1 import (
    analytics,
    auth,
    cameras,
    events,
    exports,
    playback,
    ptz,
    recordings,
    reports,
    storage,
    streams,
    system,
    users,
    ws,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(cameras.router)
api_router.include_router(streams.router)
api_router.include_router(recordings.router)
api_router.include_router(playback.router)
api_router.include_router(exports.router)
api_router.include_router(ptz.router)
api_router.include_router(events.router)
api_router.include_router(analytics.router)
api_router.include_router(storage.router)
api_router.include_router(reports.router)
api_router.include_router(system.router)
api_router.include_router(ws.router)

__all__ = ["api_router"]
