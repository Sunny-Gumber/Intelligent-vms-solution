"""Authorized, opt-in forensic jobs staged exclusively from recording exports."""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import Principal, require_roles
from app.core.config import settings
from app.db.session import get_session
from app.routers.cameras import authorized_camera
from app.routers.recordings import stream_recording_clip
from app.services.forensic_jobs import MAX_SOURCE_BYTES, forensic_jobs

def _private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(prefix="/api/v1/forensic", tags=["forensic"], dependencies=[Depends(_private_response)])


class EnhancementOptions(BaseModel):
    """Finite numeric filters; expressions and arbitrary subprocess options are forbidden."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    brightness: float = Field(0, ge=-0.2, le=0.2)
    contrast: float = Field(1, ge=0.5, le=3)
    gamma: float = Field(1, ge=0.4, le=3)
    histogram_strength: float = Field(0, ge=0, le=0.5)
    denoise: float = Field(0, ge=0, le=10)
    sharpen: float = Field(0, ge=0, le=2)
    roi: tuple[StrictInt, StrictInt, StrictInt, StrictInt] | None = None

    @field_validator("roi", mode="before")
    @classmethod
    def json_roi(cls, value):
        """Accept JSON arrays without weakening strict coordinate integer validation."""
        return tuple(value) if isinstance(value, list) else value


class EnhancementRequest(BaseModel):
    """Bind one bounded job to an authorized camera and exact finalized recording range."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    camera_id: str = Field(min_length=1, max_length=200)
    start: AwareDatetime
    duration: float = Field(gt=0, le=120, strict=True)
    settings: EnhancementOptions = Field(default_factory=EnhancementOptions)


def _enabled() -> None:
    if not settings.forensic_jobs_enabled:
        raise HTTPException(503, "Server forensic jobs are disabled")
    forensic_jobs.prepare(settings.forensic_spool_dir, settings.forensic_worker_image)


async def _authorized_job(identifier: str, session: AsyncSession, principal: Principal):
    _enabled()
    job = forensic_jobs.jobs.get(identifier)
    if job is None or job.subject != principal.subject:
        raise HTTPException(404, "Forensic job not found")
    camera = await authorized_camera(session, job.camera_id, principal)
    if (camera.tenant_id, camera.site_id) != (job.tenant_id, job.site_id):
        raise HTTPException(404, "Forensic job not found")
    return job


async def _stage(request: Request, camera, payload: EnhancementRequest, session, job) -> None:
    # Never forward an HTTP Range: a partial export is not the requested evidence interval.
    scope = dict(request.scope)
    scope["headers"] = [(name, value) for name, value in request.scope["headers"] if name.lower() != b"range"]
    response = await stream_recording_clip(
        Request(scope), camera, payload.start, payload.duration, session,
    )
    count = 0
    try:
        # Start the export generator before any local failure so its finally block
        # always owns/cleans the upstream client and export semaphore.
        first = await anext(response.body_iterator, b"")
        if response.status_code != 200:
            raise HTTPException(502, "Recording export did not return a complete clip")
        with (job.directory / "original.mp4").open("xb") as handle:
            count = len(first)
            if count > MAX_SOURCE_BYTES:
                raise HTTPException(413, "Recording export exceeds forensic source limit")
            handle.write(first)
            async for chunk in response.body_iterator:
                count += len(chunk)
                if count > MAX_SOURCE_BYTES:
                    raise HTTPException(413, "Recording export exceeds forensic source limit")
                handle.write(chunk)
        if count == 0:
            raise HTTPException(502, "Recording export was empty")
    finally:
        await response.body_iterator.aclose()


@router.post("/jobs", status_code=202)
async def create_job(
    payload: EnhancementRequest, request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Authorize and stage a bounded export, then enqueue isolated offline processing."""
    _enabled()
    camera = await authorized_camera(session, payload.camera_id, principal)
    options = payload.settings.model_dump()
    roi = options.get("roi")
    if roi and (roi[0] < 0 or roi[1] < 0 or roi[2] < 2 or roi[3] < 2 or max(roi) > 8192):
        raise HTTPException(422, "Invalid ROI bounds")
    job = forensic_jobs.reserve(camera, principal, payload.start.isoformat(), payload.duration, options)
    job.task = asyncio.current_task()
    try:
        await asyncio.wait_for(_stage(request, camera, payload, session, job), timeout=60)
        forensic_jobs.transition(job, "queued")
        job.task = asyncio.create_task(forensic_jobs.execute(job, settings.forensic_worker_image))
    except asyncio.CancelledError:
        forensic_jobs.transition(job, "cancelled")
        raise
    except Exception:
        forensic_jobs.transition(job, "failed")
        raise
    return job.snapshot()


@router.get("/jobs/{identifier}")
async def get_job(
    identifier: str, session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return job state only after fresh camera tenant/site and owner authorization."""
    return (await _authorized_job(identifier, session, principal)).snapshot()


@router.get("/jobs")
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Recover at most four owned jobs after a lost create response, rechecking scope."""
    _enabled()
    visible = []
    for identifier in tuple(forensic_jobs.jobs):
        try:
            job = await _authorized_job(identifier, session, principal)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue
            raise
        visible.append(job.snapshot())
    return {"jobs": visible}


@router.post("/jobs/{identifier}/cancel")
async def cancel_job(
    identifier: str, session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Cancel queued or running work, waiting for container termination."""
    job = await _authorized_job(identifier, session, principal)
    await forensic_jobs.cancel(job)
    return job.snapshot()


@router.get("/jobs/{identifier}/report")
async def get_report(
    identifier: str, session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Return exact worker provenance plus range/settings binding and server audit."""
    job = await _authorized_job(identifier, session, principal)
    if job.state != "completed":
        raise HTTPException(409, "Forensic job is not completed")
    return {"job": job.snapshot(), "provenance": json.loads(
        (job.directory / "enhanced.mp4.forensic.json").read_text(encoding="utf-8"),
    )}


@router.get("/jobs/{identifier}/artifacts/{kind}")
async def get_artifact(
    identifier: str, kind: str, session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Download a completed original export or derivative after fresh authorization."""
    job = await _authorized_job(identifier, session, principal)
    if kind not in {"original", "enhanced"}:
        raise HTTPException(404, "Artifact not found")
    if job.state != "completed":
        raise HTTPException(409, "Forensic job is not completed")
    return FileResponse(job.directory / (kind + ".mp4"), media_type="video/mp4",
                        filename=kind + ".mp4", headers={"Cache-Control": "private, no-store",
                                                        "X-Content-Type-Options": "nosniff"})


@router.delete("/jobs/{identifier}", status_code=204)
async def delete_job(
    identifier: str, session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Remove retained local evidence data and release the bounded admission slot."""
    job = await _authorized_job(identifier, session, principal)
    forensic_jobs.delete(job)
