"""Export creation, monitoring, download and evidence."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from vms_contracts.dto import ExportCreate, ExportOut
from vms_contracts.enums import ExportFormat, ExportState, Permission

from app.core import audit
from app.core.deps import Ctr, CurrentPrincipal, DbSession, client_ip, load_camera, rate_limit
from app.core.errors import ConflictError, NotFoundError
from app.core.paths import resolve_within
from app.db.models import ExportJob
from app.exports.evidence import build_evidence, coverage_shortfall
from app.logging_setup import get_logger

router = APIRouter(prefix="/exports", tags=["exports"])


def to_dto(job: ExportJob) -> ExportOut:
    return ExportOut(
        id=job.id, camera_id=job.camera_id, state=ExportState(job.state),
        fmt=ExportFormat(job.fmt), requested_start=job.requested_start,
        requested_end=job.requested_end, progress=job.progress,
        size_bytes=job.size_bytes, error_code=job.error_code,
        error_detail=job.error_detail, evidence=build_evidence(job),
        expires_at=job.expires_at, created_at=job.created_at)


async def _run_export(container, job_id: str) -> None:  # noqa: ANN001
    try:
        await container.exports.run(job_id)
    except Exception:  # noqa: BLE001 - the failure is already recorded on the job
        get_logger("api.exports").warning("export.background_failed", job_id=job_id)


def _load_job(session, principal, export_id: str) -> ExportJob:  # noqa: ANN001
    job = session.get(ExportJob, export_id)
    if job is None or (job.requested_by != principal.user_id and not principal.is_admin):
        raise NotFoundError("Export not found", code="export_not_found")
    return job


@router.post("", response_model=ExportOut, status_code=202,
             dependencies=[Depends(rate_limit("export"))],
             summary="Request a video export")
def create_export(request: Request, payload: ExportCreate, background: BackgroundTasks,
                  session: DbSession, container: Ctr,
                  principal: CurrentPrincipal) -> ExportOut:
    load_camera(session, principal, payload.camera_id, Permission.EXPORT_CREATE)
    job = container.exports.create(
        session, camera_id=payload.camera_id, user_id=principal.user_id,
        start=payload.start, end=payload.end, fmt=payload.fmt, reason=payload.reason,
        case_reference=payload.case_reference, actor_name=principal.username,
        client_ip=client_ip(request))
    audit.record(session, action=audit.Action.EXPORT_CREATE, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="export", object_id=job.id,
                 client_ip=client_ip(request),
                 detail={"camera_id": payload.camera_id, "reason": payload.reason,
                         "case_reference": payload.case_reference})
    session.commit()
    background.add_task(_run_export, container, job.id)
    session.refresh(job)
    return to_dto(job)


@router.get("", summary="List your exports")
def list_exports(session: DbSession, principal: CurrentPrincipal,
                 state: str | None = None, limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0)) -> dict:
    principal.require(Permission.EXPORT_CREATE)

    def apply(statement):  # noqa: ANN001
        if not principal.is_admin:
            statement = statement.where(ExportJob.requested_by == principal.user_id)
        if state:
            statement = statement.where(ExportJob.state == state)
        return statement

    total = int(session.execute(
        apply(select(func.count()).select_from(ExportJob))).scalar_one())
    rows = session.execute(
        apply(select(ExportJob)).order_by(ExportJob.created_at.desc())
        .limit(limit).offset(offset)).scalars().all()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [to_dto(j) for j in rows]}


@router.get("/{export_id}", response_model=ExportOut, summary="Export status")
def get_export(export_id: str, session: DbSession,
               principal: CurrentPrincipal) -> ExportOut:
    return to_dto(_load_job(session, principal, export_id))


@router.get("/{export_id}/evidence", summary="Evidence record and chain of custody")
def evidence(export_id: str, session: DbSession, principal: CurrentPrincipal) -> dict:
    job = _load_job(session, principal, export_id)
    record = build_evidence(job)
    if record is None:
        raise ConflictError("This export has not produced a file yet",
                            code="export_not_ready")
    return {"evidence": record, "coverage": coverage_shortfall(job)}


@router.post("/{export_id}/cancel", response_model=ExportOut, summary="Cancel an export")
async def cancel_export(request: Request, export_id: str, session: DbSession,
                        container: Ctr, principal: CurrentPrincipal) -> ExportOut:
    _load_job(session, principal, export_id)
    job = await container.exports.cancel(session, export_id, principal.user_id)
    audit.record(session, action=audit.Action.EXPORT_CANCEL, actor_id=principal.user_id,
                 actor_name=principal.username, object_type="export",
                 object_id=export_id, client_ip=client_ip(request))
    session.commit()
    session.refresh(job)
    return to_dto(job)


@router.post("/{export_id}/download-token", summary="Issue a single download token")
def download_token(request: Request, export_id: str, session: DbSession, container: Ctr,
                   principal: CurrentPrincipal) -> dict:
    principal.require(Permission.EXPORT_DOWNLOAD)
    _load_job(session, principal, export_id)
    token = container.exports.issue_download_token(session, export_id,
                                                   principal.user_id)
    audit.record(session, action=audit.Action.EXPORT_DOWNLOAD,
                 actor_id=principal.user_id, actor_name=principal.username,
                 object_type="export", object_id=export_id,
                 client_ip=client_ip(request), detail={"stage": "token_issued"})
    session.commit()
    return {"export_id": export_id, "token": token,
            "expires_in_s": container.settings.download_token_ttl_s}


@router.get("/{export_id}/download", summary="Download an exported clip")
def download(export_id: str, token: str, session: DbSession, container: Ctr,
             principal: CurrentPrincipal) -> FileResponse:
    principal.require(Permission.EXPORT_DOWNLOAD)
    # A download token does not bypass the export ownership boundary.
    _load_job(session, principal, export_id)
    job = container.exports.verify_download_token(session, export_id, token)
    if not job.relative_path:
        raise ConflictError("This export has no file", code="export_not_ready")
    path = resolve_within(container.settings.exports_dir, job.relative_path)
    if not path.is_file():
        raise NotFoundError("The export file is no longer on disk",
                            code="export_file_missing")
    filename = f"{job.camera_id}_{int(job.requested_start.timestamp())}{path.suffix}"
    session.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=filename)
