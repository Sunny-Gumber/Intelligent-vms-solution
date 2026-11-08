"""Snapshot capture: live (from the camera) and from recorded footage."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.crypto import sha256_file
from app.core.errors import MediaUnavailableError
from app.core.paths import resolve_within
from app.core.timeutil import utcnow
from app.db.models import Camera, Snapshot
from app.exports.ffmpeg_export import extract_snapshot, grab_live_snapshot
from app.logging_setup import get_logger
from app.recording import index

log = get_logger("snapshots")


class SnapshotService:
    def __init__(self, credential_store, settings: Settings | None = None) -> None:  # noqa: ANN001
        self.credentials = credential_store
        self.settings = settings or get_settings()

    def _target(self, camera_id: str, moment: dt.datetime) -> Path:
        relative = f"{camera_id}/{moment:%Y/%m/%d}/{int(moment.timestamp())}.jpg"
        path = resolve_within(self.settings.snapshots_dir, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def capture_live(self, session: Session, camera: Camera, *, actor_id: str,
                           event_id: str | None = None) -> Snapshot:
        from app.media.url_builder import build_rtsp_url, with_credentials

        stream = (next((s for s in camera.streams if s.enabled and s.role == "sub"), None)
                  or next((s for s in camera.streams if s.enabled), None))
        if stream is None:
            raise MediaUnavailableError("Camera has no enabled stream",
                                        code="no_stream_configured")

        url = stream.source_url
        if url and stream.source_url_encrypted:
            url = self.credentials.decrypt_stream_url(camera.id, url)
        if not url:
            url = build_rtsp_url(host=camera.host, port=camera.rtsp_port, path="/")
        creds = self.credentials.get(camera)
        url = with_credentials(url, creds.username, creds.password)

        moment = utcnow()
        target = self._target(camera.id, moment)
        ok = await grab_live_snapshot(
            source_url=url, output=target,
            transport="udp" if stream.transport.endswith("udp") else "tcp",
            ffmpeg_path=self.settings.ffmpeg_path)
        if not ok:
            raise MediaUnavailableError(
                "Could not capture a frame from this camera right now",
                code="snapshot_failed")
        return self._record(session, camera.id, target, moment, actor_id, "manual",
                            event_id)

    async def capture_from_recording(self, session: Session, camera_id: str,
                                     moment: dt.datetime, *, actor_id: str,
                                     event_id: str | None = None) -> Snapshot:
        segments = index.segments_in_range(session, camera_id, moment,
                                           moment + dt.timedelta(seconds=1))
        if not segments:
            raise MediaUnavailableError("No recording covers that moment",
                                        code="no_recording_at_time")
        segment = segments[0]
        source = index.resolve_path(session, segment)
        if source is None or not source.is_file():
            raise MediaUnavailableError("The recording file is missing from storage",
                                        code="media_files_missing")

        offset = max(0.0, (moment - segment.started_at).total_seconds())
        target = self._target(camera_id, moment)
        ok = await extract_snapshot(source=source, output=target, offset_s=offset,
                                    ffmpeg_path=self.settings.ffmpeg_path)
        if not ok:
            raise MediaUnavailableError(
                "Could not extract a frame from the recording", code="snapshot_failed")
        return self._record(session, camera_id, target, moment, actor_id, "recording",
                            event_id)

    def _record(self, session: Session, camera_id: str, path: Path,
                moment: dt.datetime, actor_id: str, source: str,
                event_id: str | None) -> Snapshot:
        digest, size = sha256_file(path)
        snapshot = Snapshot(
            camera_id=camera_id,
            relative_path=str(path.relative_to(self.settings.snapshots_dir)),
            taken_at=moment, taken_by=actor_id, source=source, event_id=event_id,
            size_bytes=size, sha256=digest)
        session.add(snapshot)
        session.flush()
        log.info("snapshot.captured", camera_id=camera_id, snapshot_id=snapshot.id,
                 source=source, bytes=size)
        return snapshot

    def path_for(self, snapshot: Snapshot) -> Path:
        return resolve_within(self.settings.snapshots_dir, snapshot.relative_path)
