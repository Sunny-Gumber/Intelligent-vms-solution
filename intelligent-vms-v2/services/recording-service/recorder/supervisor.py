"""Per-camera recorder supervision.

One `CameraRecorder` owns one camera's recording lifecycle:

  start -> recording -> (stream drops) -> retrying with backoff -> recording
                     -> (storage lost) -> failed + event

Guarantees implemented here (each has a test):
  * Restart delay is bounded and jittered; a flapping camera cannot spin.
  * Every completed segment is indexed before the next one is handled.
  * A detected stall kills and restarts FFmpeg rather than recording nothing.
  * Camera deletion or disable stops the recorder and finalizes the current
    segment instead of leaving a WRITING row behind.
  * Storage failure switches volume if one is available, otherwise raises a
    `recording.failed` event — never a silent stop.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, get_settings
from app.core.backoff import BackoffPolicy
from app.core.timeutil import utcnow
from app.db.models import Camera, CameraStream
from app.db.session import session_scope
from app.logging_setup import get_logger
from app.recording import health, index
from app.storage import allocator
from vms_contracts.enums import EventSeverity, EventType, RecordingState, StreamRole

from recorder.segmenter import (
    CompletedSegment,
    SegmenterConfig,
    SegmentRecorder,
    finalize_into,
)

log = get_logger("recorder.supervisor")


@dataclass
class RecorderStats:
    started_at: dt.datetime | None = None
    restarts: int = 0
    segments: int = 0
    bytes_written: int = 0
    last_error: str | None = None
    last_segment_at: dt.datetime | None = None
    state: RecordingState = RecordingState.IDLE
    events: list[dict] = field(default_factory=list)


class CameraRecorder:
    def __init__(self, camera_id: str, *, stream_role: StreamRole = StreamRole.MAIN,
                 settings: Settings | None = None, event_sink=None,
                 credential_store=None, source_override: str | None = None,
                 is_file_source: bool = False,
                 extra_input_args: list[str] | None = None) -> None:
        self.camera_id = camera_id
        self.stream_role = stream_role
        self.settings = settings or get_settings()
        self.event_sink = event_sink
        self.credentials = credential_store
        self.source_override = source_override
        self.is_file_source = is_file_source
        self.extra_input_args = list(extra_input_args or [])

        self.stats = RecorderStats()
        self.stop_event = asyncio.Event()
        self._recorder: SegmentRecorder | None = None
        self._policy = BackoffPolicy(
            initial_s=1.0, max_s=self.settings.recorder_max_restart_backoff_s,
            multiplier=2.0)

    # ------------------------------------------------------------------
    def _resolve_source(self, camera: Camera) -> tuple[str, CameraStream | None]:
        if self.source_override:
            return self.source_override, None
        stream = next((s for s in camera.streams
                       if s.role == self.stream_role.value and s.enabled), None)
        if stream is None:
            stream = next((s for s in camera.streams if s.enabled), None)
        if stream is None:
            raise RuntimeError("camera has no enabled stream to record")

        from app.media.url_builder import build_rtsp_url, with_credentials

        url = stream.source_url
        if url and stream.source_url_encrypted and self.credentials:
            url = self.credentials.decrypt_stream_url(camera.id, url)
        if not url:
            url = build_rtsp_url(host=camera.host, port=camera.rtsp_port, path="/")
        if self.credentials:
            creds = self.credentials.get(camera)
            url = with_credentials(url, creds.username, creds.password)
        return url, stream

    async def _publish(self, events: list[dict]) -> None:
        if not self.event_sink:
            self.stats.events.extend(events)
            return
        for event in events:
            await self.event_sink(event)

    # ------------------------------------------------------------------
    async def _handle_segment(self, completed: CompletedSegment) -> None:
        """Index and atomically place a completed segment."""
        segment_id = _segment_id(completed)
        with session_scope() as session:
            camera = session.get(Camera, self.camera_id)
            if camera is None:
                log.warning("recorder.segment_for_deleted_camera",
                            camera_id=self.camera_id)
                return
            try:
                allocation = allocator.allocate(
                    session, camera, role=self.stream_role.value,
                    start=completed.started_at, segment_id=segment_id,
                    required_bytes=completed.size_bytes)
            except RuntimeError as exc:
                self.stats.last_error = str(exc)
                events = health.set_state(session, camera, RecordingState.FAILED,
                                          str(exc))
                await self._publish(events)
                return

            segment = index.open_segment(
                session, camera_id=camera.id, camera_name=camera.name,
                role=self.stream_role.value, volume_id=allocation.volume_id,
                relative_path=allocation.relative_path,
                started_at=completed.started_at, reason="continuous",
                segment_id=segment_id, has_gap_before=self._gap_before(completed))

            try:
                size = finalize_into(completed.path, allocation.absolute_path)
            except OSError as exc:
                index.mark_corrupt(session, segment.id, f"move failed: {exc}")
                self.stats.last_error = str(exc)
                return

            index.finalize_segment(session, segment.id, ended_at=completed.ended_at,
                                   size_bytes=size)
            self.stats.segments += 1
            self.stats.bytes_written += size
            self.stats.last_segment_at = utcnow()

        log.info("recorder.segment_indexed", camera_id=self.camera_id,
                 size=completed.size_bytes, duration_s=round(completed.duration_s, 1))

    def _gap_before(self, completed: CompletedSegment) -> bool:
        if self.stats.last_segment_at is None:
            return self.stats.restarts > 0
        delta = (completed.started_at - self.stats.last_segment_at).total_seconds()
        return delta > self.settings.gap_tolerance_s

    # ------------------------------------------------------------------
    async def run(self) -> None:
        attempt = 0
        self.stats.started_at = utcnow()

        while not self.stop_event.is_set():
            try:
                source, stream = self._prepare()
            except Exception as exc:  # noqa: BLE001
                await self._fail(str(exc))
                return

            spool = (Path(self.settings.data_root) / "spool" / self.camera_id
                     / self.stream_role.value)
            config = SegmenterConfig(
                source_url=source, spool_dir=spool,
                segment_seconds=self.settings.segment_seconds,
                transport=("udp" if (stream and stream.transport.endswith("udp"))
                           else "tcp"),
                ffmpeg_path=self.settings.ffmpeg_path,
                record_audio=bool(stream.audio_codec) if stream else False,
                is_file_source=self.is_file_source,
                extra_input_args=self.extra_input_args)
            self._recorder = SegmentRecorder(config, self._handle_segment,
                                             name=f"cam:{self.camera_id}")

            await self._set_state(RecordingState.RECORDING, None)
            watchdog = asyncio.create_task(self._watchdog())
            try:
                returncode = await self._recorder.run()
            except FileNotFoundError as exc:
                await self._fail(str(exc))
                return
            finally:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)

            if self.stop_event.is_set():
                break

            attempt += 1
            self.stats.restarts += 1
            detail = (f"ffmpeg exited with code {returncode}: "
                      f"{self._recorder.stderr_tail}")[:480]
            self.stats.last_error = detail
            await self._set_state(RecordingState.RETRYING, detail)

            delay = self._policy.delay_for(attempt)
            log.warning("recorder.restarting", camera_id=self.camera_id,
                        attempt=attempt, delay_s=round(delay, 2),
                        detail=detail[:200])
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                break
            except TimeoutError:
                continue

        await self._set_state(RecordingState.IDLE, "stopped")

    def _prepare(self) -> tuple[str, CameraStream | None]:
        with session_scope() as session:
            camera = session.get(Camera, self.camera_id)
            if camera is None:
                raise RuntimeError("camera no longer exists")
            if not camera.enabled:
                raise RuntimeError("camera is disabled")
            source, stream = self._resolve_source(camera)
            if stream is not None:
                session.expunge(stream)
            return source, stream

    async def _watchdog(self) -> None:
        """Kill a recorder that stopped producing segments."""
        interval = max(5.0, self.settings.segment_seconds / 4)
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            recorder = self._recorder
            if recorder and recorder.is_stalled(self.settings.recorder_stall_timeout_s):
                log.warning("recorder.stalled", camera_id=self.camera_id)
                self.stats.last_error = (
                    "stream stalled: no segment within the expected interval")
                await recorder.stop(timeout_s=5.0)
                return

    async def _set_state(self, state: RecordingState, detail: str | None) -> None:
        self.stats.state = state
        events: list[dict] = []
        with session_scope() as session:
            camera = session.get(Camera, self.camera_id)
            if camera is not None:
                events = health.set_state(session, camera, state, detail)
        await self._publish(events)

    async def _fail(self, detail: str) -> None:
        self.stats.last_error = detail
        log.error("recorder.failed", camera_id=self.camera_id, detail=detail)
        await self._set_state(RecordingState.FAILED, detail)
        await self._publish([{
            "type": EventType.RECORDING_FAILED,
            "severity": EventSeverity.HIGH,
            "camera_id": self.camera_id,
            "payload": {"detail": detail},
        }])

    async def stop(self) -> None:
        self.stop_event.set()
        if self._recorder is not None:
            await self._recorder.stop()


def _segment_id(completed: CompletedSegment) -> str:
    raw = f"{completed.path.name}:{completed.started_at.timestamp()}"
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:32]
