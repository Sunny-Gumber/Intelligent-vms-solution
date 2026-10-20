"""Export pipeline: clip extraction, evidence hashing, chain of custody.

Guarantees (each covered by a test):

  * Pre-flight checks disk space BEFORE starting, estimating from the actual
    source segment sizes plus a configurable headroom factor.
  * Concurrency is bounded by a semaphore.
  * Stream copy is preferred; evidence is not re-encoded unless asked.
  * The output is SHA-256 hashed the moment it is finalized, and the hash plus
    a chain-of-custody list is stored with the job.
  * Cancellation terminates FFmpeg and deletes the partial file, leaving the
    job CANCELLED — never a half file that looks complete.
  * Missing source footage FAILS the export with a precise error instead of
    producing a shorter clip that appears to cover the requested window.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vms_contracts.enums import ExportFormat, ExportState, StreamRole

from app.config import Settings, get_settings
from app.core.crypto import constant_time_equals, hash_token, new_opaque_token, sha256_file
from app.core.errors import CapacityError, ConflictError, NotFoundError, ValidationError
from app.core.paths import resolve_within
from app.core.timeutil import ensure_utc, utcnow
from app.db.models import ExportJob
from app.db.session import session_scope
from app.exports import ffmpeg_export
from app.exports.evidence import append_custody, build_evidence
from app.exports.preflight import PreflightResult, preflight
from app.logging_setup import get_logger
from app.playback.resolver import resolve_media

log = get_logger("exports")


@dataclass
class ExportContext:
    job_id: str
    output_path: Path
    cancel_event: asyncio.Event


class ExportService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_exports)
        self._running: dict[str, ExportContext] = {}

    # ------------------------------------------------------------------
    def create(self, session: Session, *, camera_id: str, user_id: str,
               start: dt.datetime, end: dt.datetime,
               fmt: ExportFormat = ExportFormat.MP4, reason: str | None = None,
               case_reference: str | None = None, actor_name: str | None = None,
               client_ip: str | None = None) -> ExportJob:
        start, end = ensure_utc(start), ensure_utc(end)
        if end <= start:
            raise ValidationError("The end time must be after the start time",
                                  code="invalid_time_range")
        duration = (end - start).total_seconds()
        if duration > self.settings.export_max_duration_s:
            raise ValidationError(
                f"Exports are limited to {self.settings.export_max_duration_s // 3600} "
                f"hours; you requested {duration / 3600:.1f} hours.",
                code="export_too_long")

        queued = int(session.execute(
            select(func.count()).select_from(ExportJob).where(
                ExportJob.state.in_((ExportState.QUEUED.value, ExportState.RUNNING.value,
                                     ExportState.PREFLIGHT.value)))).scalar_one())
        if queued >= self.settings.max_concurrent_exports * 4:
            raise CapacityError(
                "Too many exports are queued. Wait for current exports to finish.",
                code="export_queue_full")

        job = ExportJob(
            camera_id=camera_id, requested_by=user_id, requested_start=start,
            requested_end=end, fmt=fmt.value, state=ExportState.QUEUED.value,
            reason=reason, case_reference=case_reference,
            expires_at=utcnow() + dt.timedelta(hours=self.settings.export_ttl_hours))
        job.chain_of_custody = append_custody(
            [], action="requested", actor=user_id, actor_name=actor_name,
            client_ip=client_ip,
            detail={"start": start.isoformat(), "end": end.isoformat()})
        session.add(job)
        session.flush()
        log.info("export.created", job_id=job.id, camera_id=camera_id, user_id=user_id)
        return job

    # ------------------------------------------------------------------
    async def run(self, job_id: str) -> None:
        """Execute one export job. Safe to call concurrently for distinct jobs."""
        async with self._semaphore:
            cancel = asyncio.Event()
            with session_scope() as session:
                job = session.get(ExportJob, job_id)
                if job is None:
                    raise NotFoundError("Export job not found", code="export_not_found")
                if job.cancel_requested:
                    job.state = ExportState.CANCELLED.value
                    return
                if job.state not in (ExportState.QUEUED.value,
                                     ExportState.PREFLIGHT.value):
                    return
                job.state = ExportState.PREFLIGHT.value
                camera_id = job.camera_id
                start, end = job.requested_start, job.requested_end
                fmt = ExportFormat(job.fmt)
                requested_by = job.requested_by

            try:
                with session_scope() as session:
                    media = resolve_media(session, camera_id, start, end,
                                          role=StreamRole.MAIN, require_complete=True)
                    check: PreflightResult = preflight(
                        media.paths, self.settings.exports_dir,
                        headroom=self.settings.export_disk_headroom_factor)
                    segment_ids = [s.id for s in media.segments]
                    sources = list(media.paths)
                    actual_start, actual_end = media.actual_start, media.actual_end
                    skip_seconds = max(
                        0.0, (start - media.segments[0].started_at).total_seconds())

                if not check.ok:
                    await self._fail(job_id, "insufficient_space", check.reason or "")
                    return

                output_dir = resolve_within(self.settings.exports_dir, job_id)
                output_dir.mkdir(parents=True, exist_ok=True)
                extension = ("mp4" if fmt is not ExportFormat.ORIGINAL
                             else (sources[0].suffix.lstrip(".") or "mp4"))
                output_path = output_dir / f"export_{job_id}.{extension}"

                self._running[job_id] = ExportContext(job_id, output_path, cancel)
                await self._mark_running(job_id)

                async def progress(fraction: float) -> None:
                    await self._update_progress(job_id, fraction)

                result = await ffmpeg_export.export_clip(
                    sources=sources, output=output_path, skip_seconds=skip_seconds,
                    duration_seconds=(end - start).total_seconds(), transcode=False,
                    ffmpeg_path=self.settings.ffmpeg_path, cancel=cancel,
                    on_progress=progress)

                if result.cancelled:
                    await self._finish_cancelled(job_id, output_path)
                    return
                if not result.ok:
                    await self._fail(job_id, "ffmpeg_failed",
                                     result.error or "export failed")
                    return

                digest, size = sha256_file(output_path)
                with session_scope() as session:
                    job = session.get(ExportJob, job_id)
                    if job is None:
                        return
                    job.state = ExportState.COMPLETED.value
                    job.progress = 1.0
                    job.relative_path = str(
                        output_path.relative_to(self.settings.exports_dir))
                    job.size_bytes = size
                    job.sha256 = digest
                    job.actual_start = actual_start
                    job.actual_end = actual_end
                    job.segment_ids = segment_ids
                    job.chain_of_custody = append_custody(
                        job.chain_of_custody, action="produced", actor=requested_by,
                        detail={"sha256": digest, "size_bytes": size,
                                "segments": len(segment_ids)})
                    job.version += 1
                log.info("export.completed", job_id=job_id, size=size,
                         sha256=digest[:16])
            except Exception as exc:  # noqa: BLE001
                code = getattr(exc, "code", "export_failed")
                await self._fail(job_id, code, str(exc)[:500])
                raise
            finally:
                self._running.pop(job_id, None)

    # ------------------------------------------------------------------
    async def cancel(self, session: Session, job_id: str, actor: str) -> ExportJob:
        job = session.get(ExportJob, job_id)
        if job is None:
            raise NotFoundError("Export job not found", code="export_not_found")
        if job.state in (ExportState.COMPLETED.value, ExportState.FAILED.value,
                         ExportState.CANCELLED.value):
            raise ConflictError(f"Export is already {job.state}",
                                code="export_finished")
        job.cancel_requested = True
        job.chain_of_custody = append_custody(job.chain_of_custody,
                                              action="cancel_requested", actor=actor)
        context = self._running.get(job_id)
        if context:
            context.cancel_event.set()
        else:
            job.state = ExportState.CANCELLED.value
        return job

    def issue_download_token(self, session: Session, job_id: str, actor: str) -> str:
        job = session.get(ExportJob, job_id)
        if job is None:
            raise NotFoundError("Export job not found", code="export_not_found")
        if job.state != ExportState.COMPLETED.value:
            raise ConflictError(f"Export is {job.state}; nothing to download",
                                code="export_not_ready")
        raw = new_opaque_token(24)
        job.download_token_hash = hash_token(raw, self.settings.secret_key)
        job.chain_of_custody = append_custody(job.chain_of_custody,
                                              action="download_token_issued", actor=actor)
        return raw

    def verify_download_token(self, session: Session, job_id: str,
                              token: str) -> ExportJob:
        job = session.get(ExportJob, job_id)
        if job is None or not job.download_token_hash:
            raise NotFoundError("Export not available", code="export_not_found")
        if job.expires_at and job.expires_at <= utcnow():
            raise ConflictError("This export has expired", code="export_expired")
        if not constant_time_equals(job.download_token_hash,
                                    hash_token(token, self.settings.secret_key)):
            raise NotFoundError("Export not available", code="export_not_found")
        job.download_count += 1
        job.chain_of_custody = append_custody(job.chain_of_custody, action="downloaded",
                                              actor=job.requested_by)
        return job

    def evidence(self, job: ExportJob):
        return build_evidence(job)

    # -- internal state transitions ------------------------------------
    async def _mark_running(self, job_id: str) -> None:
        with session_scope() as session:
            job = session.get(ExportJob, job_id)
            if job:
                job.state = ExportState.RUNNING.value
                job.version += 1

    async def _update_progress(self, job_id: str, fraction: float) -> None:
        with session_scope() as session:
            job = session.get(ExportJob, job_id)
            if job and job.state == ExportState.RUNNING.value:
                job.progress = round(min(max(fraction, 0.0), 0.99), 3)

    async def _fail(self, job_id: str, code: str, detail: str) -> None:
        with session_scope() as session:
            job = session.get(ExportJob, job_id)
            if job is None:
                return
            job.state = ExportState.FAILED.value
            job.error_code = code
            job.error_detail = detail[:1000]
            job.chain_of_custody = append_custody(job.chain_of_custody, action="failed",
                                                  actor="system", detail={"code": code})
            job.version += 1
        log.error("export.failed", job_id=job_id, code=code, detail=detail[:200])

    async def _finish_cancelled(self, job_id: str, output: Path) -> None:
        try:
            if await asyncio.to_thread(output.is_file):
                await asyncio.to_thread(output.unlink)
        except OSError:
            pass
        with session_scope() as session:
            job = session.get(ExportJob, job_id)
            if job:
                job.state = ExportState.CANCELLED.value
                job.progress = 0.0
                job.chain_of_custody = append_custody(job.chain_of_custody,
                                                      action="cancelled", actor="system")
                job.version += 1
        log.info("export.cancelled", job_id=job_id)
