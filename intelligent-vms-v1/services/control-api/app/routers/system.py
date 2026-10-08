from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.core.auth import Principal, require_roles
from app.core.config import settings
from app.db.session import SessionLocal
from app.observability import READY
from app.services.mediamtx import mediamtx
from app.services.reconciler import stats
from app.services.outbox import outbox_counts, requeue_dead

router = APIRouter(prefix="/api/v1/system", tags=["system"])


@router.get("/healthz/live", include_in_schema=False)
async def liveness():
    """Return process liveness for orchestrator health checks.

    Returns:
        Static status object indicating that the API process is running.
    """
    return {"status": "ok"}


@router.get("/healthz/ready", include_in_schema=False)
async def readiness():
    """Verify database readiness and update the readiness metric.

    Returns:
        Ready/database status when the database probe succeeds.

    Raises:
        HTTPException: HTTP 503 when the database probe fails.
    """
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        READY.set(1)
        return {"status": "ready", "database": "ok"}
    except Exception as exc:
        READY.set(0)
        raise HTTPException(503, "database unavailable") from exc


@router.get("/health")
async def health():
    """Return basic media-node health without requiring authentication.

    Returns:
        Status object reporting overall/media-node health. A raised walk, a
        truncated walk, or an inconsistent walk is degraded rather than ok.
    """
    media = "ok"
    try:
        listed = await mediamtx.list_paths()
        if isinstance(listed, dict) and (
            listed.get("truncated") is True or listed.get("inconsistent") is True
        ):
            media = "degraded"
    except Exception:
        media = "degraded"
    return {"status": "ok" if media == "ok" else "degraded", "media_node": media}


@router.get("/capabilities")
async def capabilities(
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return deployment-profile capability flags for the authenticated UI."""
    return {
        "deployment_profile": settings.deployment_profile,
        "event_pipeline": settings.event_pipeline_enabled,
        "event_history": settings.event_history_enabled,
        "alarm_processing": settings.alarm_processing_enabled,
        "ai_ui": settings.ai_ui_enabled,
        "distributed_placement": settings.placement_execution_enabled,
    }


@router.get("/reconciliation")
async def reconciliation_status(
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return the latest media-reconciliation execution statistics.

    Args:
        principal: Authorized administrator or operator.

    Returns:
        Reconciler run counters, cursor and latest duration/failure values.
    """
    return {
        "total_runs": stats.total_runs,
        "last_duration_seconds": stats.last_duration_seconds,
        "last_scanned": stats.last_scanned,
        "last_changed": stats.last_changed,
        "last_failed": stats.last_failed,
        "last_cursor": stats.last_cursor,
    }



@router.get("/outbox")
async def outbox_status(
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return transactional-outbox status counts.

    Args:
        principal: Authorized administrator or operator.

    Returns:
        Current outbox state counters from the outbox service.

    Raises:
        Exception: Database/outbox query failures propagate to the API handler.
    """
    return await outbox_counts()


@router.post("/outbox/{message_id}/requeue")
async def outbox_requeue(
    message_id: str,
    principal: Principal = Depends(require_roles("admin")),
):
    """Requeue one dead-letter outbox message for delivery retry.

    Args:
        message_id: Durable outbox message identifier.
        principal: Authorized administrator.

    Returns:
        Confirmation object when the message is requeued.

    Raises:
        HTTPException: HTTP 404 when no dead-letter message matches the identifier.
    """
    if not await requeue_dead(message_id):
        raise HTTPException(404, "Dead-letter outbox item not found")
    return {"requeued": True, "id": message_id}
