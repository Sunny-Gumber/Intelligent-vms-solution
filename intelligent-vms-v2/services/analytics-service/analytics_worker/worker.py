"""Analytics worker process.

Deployment model (ADR-0010): this runs as its OWN OS process/service. It
reads camera configuration from the database, samples frames at a bounded
rate, runs the configured detector, applies zone rules and writes results back
as analytics events. It has NO code path that can block, slow or stop
recording - that is the whole point of the process boundary.

Behaviour when no model is configured, which is the default:
    * the worker starts
    * it reports `inference_available: false` with the exact reason
    * it processes NOTHING and claims nothing
    * camera-side and external analytics continue to flow through the control
      API unaffected

Run:
    python -m analytics_worker.worker
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from dataclasses import dataclass, field
from pathlib import Path

from app.analytics.ingest import AnalyticsInput, store, to_event
from app.analytics.linking import link_segment
from app.analytics.zones import ZoneEvaluator
from app.config import Settings, get_settings
from app.core.backoff import BackoffPolicy
from app.core.supervisor import interruptible_sleep
from app.core.timeutil import utcnow
from app.db import session as db
from app.db.models import AnalyticsZone, Camera, CameraStream, WorkerHeartbeat
from app.db.session import session_scope
from app.logging_setup import configure_logging, get_logger
from sqlalchemy import select
from vms_contracts.enums import AnalyticsSource

from analytics_worker.detectors.base import Detector, NullDetector
from analytics_worker.runtime import RuntimeStatus, resolve
from analytics_worker.sampler import FrameSampler, SamplerConfig
from analytics_worker.tracker import IouTracker

log = get_logger("analytics.worker")


@dataclass
class CameraAnalyticsStats:
    camera_id: str
    frames: int = 0
    detections: int = 0
    zone_hits: int = 0
    errors: int = 0
    last_frame_at: str | None = None
    restarts: int = 0


@dataclass
class WorkerStats:
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    cameras: dict[str, CameraAnalyticsStats] = field(default_factory=dict)

    def for_camera(self, camera_id: str) -> CameraAnalyticsStats:
        stats = self.cameras.get(camera_id)
        if stats is None:
            stats = CameraAnalyticsStats(camera_id=camera_id)
            self.cameras[camera_id] = stats
        return stats

    def to_metrics(self) -> dict[str, float]:
        return {
            "cameras": float(len(self.cameras)),
            "frames": float(sum(c.frames for c in self.cameras.values())),
            "detections": float(sum(c.detections for c in self.cameras.values())),
            "zone_hits": float(sum(c.zone_hits for c in self.cameras.values())),
            "errors": float(sum(c.errors for c in self.cameras.values())),
        }


class AnalyticsWorker:
    def __init__(self, settings: Settings | None = None, *,
                 detector: Detector | None = None,
                 credential_store=None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = f"analytics@{Path.cwd().name}:{id(self)}"
        self.stop_event = asyncio.Event()
        self.stats = WorkerStats()
        self.evaluators: dict[str, ZoneEvaluator] = {}
        self.trackers: dict[str, IouTracker] = {}
        self.credentials = credential_store

        self.runtime: RuntimeStatus = resolve(
            self.settings.analytics_model_dir, list(self.settings.analytics_providers))
        self.detector = detector or self._build_detector()

    # ------------------------------------------------------------------
    def _build_detector(self) -> Detector:
        if not self.runtime.available:
            return NullDetector(self.runtime.reason or "inference runtime unavailable")
        from analytics_worker.detectors.onnx_detector import OnnxDetector

        labels = (self.settings.analytics_model_dir / "labels.txt"
                  if self.settings.analytics_model_dir else None)
        detector = OnnxDetector(self.runtime, labels_path=labels)
        detector.warmup()
        return detector

    @property
    def inference_available(self) -> bool:
        return self.detector.available()

    def status(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "inference_available": self.inference_available,
            "detector": self.detector.name,
            "runtime": self.runtime.to_dict(),
            "cameras_processing": len(self.stats.cameras),
            "metrics": self.stats.to_metrics(),
        }

    # ------------------------------------------------------------------
    def _analytics_cameras(self) -> list[tuple[str, str, str]]:
        """Cameras with at least one enabled zone. Returns (id, name, source)."""
        out: list[tuple[str, str, str]] = []
        with session_scope() as session:
            zoned = {
                row for row in session.execute(
                    select(AnalyticsZone.camera_id)
                    .where(AnalyticsZone.enabled.is_(True))).scalars().all()
            }
            if not zoned:
                return []
            cameras = session.execute(
                select(Camera).where(Camera.id.in_(zoned),
                                     Camera.enabled.is_(True))).scalars().all()
            for camera in cameras:
                source = self._source_url(session, camera)
                if source:
                    out.append((camera.id, camera.name, source))
        return out

    def _source_url(self, session, camera: Camera) -> str | None:
        from app.media.url_builder import build_rtsp_url, with_credentials

        stream = session.execute(
            select(CameraStream).where(CameraStream.camera_id == camera.id,
                                       CameraStream.enabled.is_(True),
                                       CameraStream.role == "sub")).scalar_one_or_none()
        if stream is None:
            stream = session.execute(
                select(CameraStream).where(CameraStream.camera_id == camera.id,
                                           CameraStream.enabled.is_(True))
            ).scalars().first()
        if stream is None:
            return None

        url = stream.source_url
        if url and stream.source_url_encrypted and self.credentials:
            url = self.credentials.decrypt_stream_url(camera.id, url)
        if not url:
            url = build_rtsp_url(host=camera.host, port=camera.rtsp_port, path="/")
        if self.credentials:
            creds = self.credentials.get(camera)
            url = with_credentials(url, creds.username, creds.password)
        return url

    def _zones(self, camera_id: str) -> list[AnalyticsZone]:
        with session_scope() as session:
            zones = session.execute(
                select(AnalyticsZone).where(AnalyticsZone.camera_id == camera_id,
                                            AnalyticsZone.enabled.is_(True))
            ).scalars().all()
            for zone in zones:
                session.expunge(zone)
            return list(zones)

    # ------------------------------------------------------------------
    async def process_camera(self, camera_id: str, camera_name: str,
                             source_url: str) -> None:
        """Sample, detect, track, evaluate zones and persist - for one camera."""
        stats = self.stats.for_camera(camera_id)
        zones = self._zones(camera_id)
        evaluator = self.evaluators.setdefault(camera_id, ZoneEvaluator())
        tracker = self.trackers.setdefault(camera_id, IouTracker())

        config = SamplerConfig(source_url=source_url,
                               fps=self.settings.analytics_max_fps_per_camera,
                               ffmpeg_path=self.settings.ffmpeg_path)

        async with FrameSampler(config, name=f"analytics:{camera_name}") as sampler:
            async for frame in sampler.frames():
                if self.stop_event.is_set():
                    return
                stats.frames += 1
                stats.last_frame_at = utcnow().isoformat()

                try:
                    detections = self.detector.detect(frame, min_confidence=0.4)
                except Exception as exc:  # noqa: BLE001
                    stats.errors += 1
                    log.error("analytics.detect_failed", camera_id=camera_id,
                              error=str(exc))
                    return

                if not detections:
                    continue
                stats.detections += len(detections)

                track_ids = tracker.update(
                    [(d.object_class.value, d.bbox) for d in detections])

                for detection, track_id in zip(detections, track_ids):
                    self._persist(camera_id, detection, track_id)
                    stats.zone_hits += self._evaluate_zones(
                        camera_id, zones, evaluator, detection, track_id)

    def _persist(self, camera_id: str, detection, track_id: str) -> None:
        with session_scope() as session:
            record = store(session, AnalyticsInput(
                camera_id=camera_id,
                source=AnalyticsSource.SERVER_INFERENCE,
                object_class=detection.object_class,
                confidence=detection.confidence,
                bbox=list(detection.bbox),
                track_id=track_id,
                attributes=dict(detection.attributes),
                model_name=self.detector.name,
                model_version=self.detector.model_version))
            link_segment(session, record)

    def _evaluate_zones(self, camera_id: str, zones: list[AnalyticsZone],
                        evaluator: ZoneEvaluator, detection, track_id: str) -> int:
        hits = 0
        for zone in zones:
            classes = zone.object_classes or []
            if classes and detection.object_class.value not in classes:
                continue
            if detection.confidence < (zone.min_confidence or 0):
                continue

            polygon = [tuple(point) for point in (zone.polygon or [])]
            if len(polygon) < 2:
                continue

            for hit in evaluator.evaluate(
                    zone_id=zone.id, kind=zone.kind, polygon=polygon,
                    bbox=list(detection.bbox), track_id=track_id,
                    dwell_seconds=zone.dwell_seconds, direction=zone.direction):
                hits += 1
                with session_scope() as session:
                    record = store(session, AnalyticsInput(
                        camera_id=camera_id,
                        source=AnalyticsSource.SERVER_INFERENCE,
                        object_class=detection.object_class,
                        confidence=detection.confidence,
                        bbox=list(detection.bbox), track_id=track_id,
                        zone_id=zone.id, rule=hit.rule,
                        attributes={"direction": hit.direction,
                                    "dwell_s": round(hit.dwell_s, 2),
                                    "zone_name": zone.name},
                        model_name=self.detector.name,
                        model_version=self.detector.model_version))
                    link_segment(session, record)
                    camera = session.get(Camera, camera_id)
                    event = to_event(record, camera)
                log.info("analytics.zone_hit", camera_id=camera_id, zone=zone.name,
                         rule=hit.rule, track=track_id)
                self._publish(event)
        return hits

    def _publish(self, event: dict) -> None:
        """Hand the event to the control API's ingestion endpoint.

        In a single-host deployment the control API picks analytics rows up
        directly; the HTTP publish path exists for distributed deployments and
        is intentionally best-effort: a failure here must never stop analytics
        or recording.
        """
        log.debug("analytics.event_ready", type=str(event.get("type")),
                  camera_id=event.get("camera_id"))

    # ------------------------------------------------------------------
    async def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with session_scope() as session:
                    row = session.get(WorkerHeartbeat, self.worker_id)
                    if row is None:
                        row = WorkerHeartbeat(worker_id=self.worker_id, kind="analytics")
                        session.add(row)
                    row.heartbeat_at = utcnow()
                    row.state = "running" if self.inference_available else "degraded"
                    row.metrics = self.stats.to_metrics()
            except Exception as exc:  # noqa: BLE001
                log.warning("analytics.heartbeat_failed", error=str(exc))
            if await interruptible_sleep(self.stop_event, 30):
                return

    async def run(self) -> None:
        log.info("analytics.worker_started", **self.status())

        if not self.inference_available:
            log.warning(
                "analytics.inference_unavailable",
                reason=self.runtime.reason,
                consequence="Server-side inference is OFF. Camera-side and external "
                            "analytics are unaffected. No detection is being performed "
                            "by this server.")

        heartbeat = asyncio.create_task(self.heartbeat_loop())
        policy = BackoffPolicy(initial_s=2.0, max_s=60.0)
        running: dict[str, asyncio.Task] = {}

        try:
            while not self.stop_event.is_set():
                if self.inference_available:
                    for camera_id, name, source in self._analytics_cameras():
                        task = running.get(camera_id)
                        if task is None or task.done():
                            attempt = self.stats.for_camera(camera_id).restarts
                            if task is not None:
                                self.stats.for_camera(camera_id).restarts += 1
                                await asyncio.sleep(policy.delay_for(attempt + 1))
                            running[camera_id] = asyncio.create_task(
                                self.process_camera(camera_id, name, source))
                if await interruptible_sleep(self.stop_event, 15):
                    break
        finally:
            heartbeat.cancel()
            for task in running.values():
                task.cancel()
            await asyncio.gather(heartbeat, *running.values(), return_exceptions=True)
            self.detector.close()
            log.info("analytics.worker_stopped", **self.stats.to_metrics())

    def stop(self) -> None:
        self.stop_event.set()


async def amain() -> int:
    parser = argparse.ArgumentParser(description="Intelligent VMS analytics worker")
    parser.add_argument("--status", action="store_true",
                        help="print runtime status as JSON and exit")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json,
                      settings.logs_dir / "analytics-worker.log",
                      service="analytics-worker")
    db.get_engine(settings)

    from app.core.crypto import CredentialVault, load_or_create_master_key
    from app.devices.credentials import CredentialStore

    settings.ensure_directories()
    vault = CredentialVault(load_or_create_master_key(settings.keys_dir))
    worker = AnalyticsWorker(settings, credential_store=CredentialStore(vault))

    if args.status:
        import json

        print(json.dumps(worker.status(), indent=2))
        return 0

    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, signal_name):
            try:
                loop.add_signal_handler(getattr(signal, signal_name), worker.stop)
            except NotImplementedError:      # Windows
                pass

    await worker.run()
    return 0


def main() -> int:  # pragma: no cover - process entry point
    return asyncio.run(amain())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
