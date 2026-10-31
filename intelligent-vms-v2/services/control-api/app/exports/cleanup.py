"""Export expiry and disk reclamation."""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vms_contracts.enums import ExportState

from app.config import Settings, get_settings
from app.core.paths import resolve_within
from app.core.timeutil import utcnow
from app.db.models import ExportJob
from app.logging_setup import get_logger

log = get_logger("exports.cleanup")


@dataclass
class CleanupResult:
    expired: int = 0
    bytes_reclaimed: int = 0
    errors: int = 0


def cleanup_expired(session: Session, settings: Settings | None = None) -> CleanupResult:
    settings = settings or get_settings()
    result = CleanupResult()
    now = utcnow()

    jobs = session.execute(
        select(ExportJob).where(ExportJob.expires_at.is_not(None),
                                ExportJob.expires_at <= now,
                                ExportJob.state != ExportState.EXPIRED.value)
    ).scalars().all()

    for job in jobs:
        if job.relative_path:
            try:
                path = resolve_within(settings.exports_dir, job.relative_path)
                if path.is_file():
                    result.bytes_reclaimed += path.stat().st_size
                    path.unlink()
                parent = path.parent
                if (parent != settings.exports_dir and parent.is_dir()
                        and not any(parent.iterdir())):
                    shutil.rmtree(parent, ignore_errors=True)
            except Exception as exc:  # noqa: BLE001
                result.errors += 1
                log.warning("export.cleanup_failed", job_id=job.id, error=str(exc))
        job.state = ExportState.EXPIRED.value
        job.relative_path = None
        job.download_token_hash = None
        result.expired += 1

    if result.expired:
        log.info("export.cleanup", expired=result.expired,
                 bytes=result.bytes_reclaimed)
    return result
