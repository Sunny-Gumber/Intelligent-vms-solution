"""Composition root.

All cross-cutting services are constructed once, here, and injected. No module
reaches for a global service; FastAPI routes receive the container through a
dependency. This is what makes the system testable: a test builds a container
with a NullGatewayClient and a fake adapter factory and then exercises the
real code paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.analytics.zones import ZoneEvaluator
from app.config import Settings, get_settings
from app.core.crypto import CredentialVault, load_or_create_master_key
from app.core.netguard import GuardPolicy, NetworkGuard
from app.core.ratelimit import LimiterRegistry
from app.core.supervisor import Supervisor
from app.devices.credentials import CredentialStore
from app.devices.monitor import DeviceMonitor
from app.devices.probe import DeviceProber
from app.events_engine.ingest import EventPipeline
from app.exports.jobs import ExportService
from app.federation.aggregator import FederationAggregator
from app.health.registry import HealthRegistry
from app.licensing.entitlements import Entitlements, resolve as resolve_entitlements
from app.logging_setup import get_logger
from app.media.mediamtx_client import MediaGatewayClient, NullGatewayClient
from app.media.session_broker import SessionBroker
from app.notifications.dispatcher import NotificationDispatcher
from app.ptz.arbiter import PtzArbiter
from app.ptz.service import PtzService
from app.services.snapshots import SnapshotService
from app.storage.monitor import StorageMonitor

log = get_logger("container")


@dataclass
class Container:
    settings: Settings
    vault: CredentialVault
    guard: NetworkGuard
    credentials: CredentialStore
    gateway: MediaGatewayClient
    sessions: SessionBroker
    prober: DeviceProber
    monitor: DeviceMonitor
    events: EventPipeline
    exports: ExportService
    snapshots: SnapshotService
    ptz: PtzService
    storage_monitor: StorageMonitor
    notifications: NotificationDispatcher
    federation: FederationAggregator
    health: HealthRegistry
    supervisor: Supervisor
    limiters: LimiterRegistry
    entitlements: Entitlements
    zone_evaluators: dict[str, ZoneEvaluator] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def zone_evaluator(self, camera_id: str) -> ZoneEvaluator:
        evaluator = self.zone_evaluators.get(camera_id)
        if evaluator is None:
            evaluator = ZoneEvaluator()
            self.zone_evaluators[camera_id] = evaluator
        return evaluator

    def refresh_entitlements(self) -> Entitlements:
        self.entitlements = resolve_entitlements(self.settings)
        return self.entitlements


def build_container(settings: Settings | None = None, *,
                    gateway: MediaGatewayClient | None = None,
                    adapter_factory=None) -> Container:  # noqa: ANN001
    settings = settings or get_settings()
    settings.ensure_directories()

    vault = CredentialVault(load_or_create_master_key(settings.keys_dir))
    guard = NetworkGuard(GuardPolicy.from_settings(settings))
    credentials = CredentialStore(vault)

    media_gateway = gateway or (
        MediaGatewayClient(settings.media_gateway_url,
                           username=settings.media_gateway_user,
                           password=settings.media_gateway_password)
        if settings.media_gateway_url else NullGatewayClient())

    prober = DeviceProber(guard, credentials, settings)
    broker = SessionBroker(media_gateway, credentials, settings)
    events = EventPipeline(settings)
    exports = ExportService(settings)
    snapshots = SnapshotService(credentials, settings)

    async def default_adapter_factory(session: Session, camera):  # noqa: ANN001
        return await prober.build(camera)

    ptz = PtzService(adapter_factory or default_adapter_factory,
                     PtzArbiter(settings), settings)

    async def event_sink(event: dict) -> None:
        await events.submit(event)

    monitor = DeviceMonitor(prober, event_sink, settings)
    storage_monitor = StorageMonitor(event_sink, settings)
    notifications = NotificationDispatcher(vault)
    federation = FederationAggregator(vault, settings)

    limiters = LimiterRegistry()
    limiters.configure("login", settings.rate_limit_login_per_minute, burst=10)
    limiters.configure("api", settings.rate_limit_api_per_minute, burst=120)
    limiters.configure("export", 30, burst=10)
    limiters.configure("discovery", 6, burst=3)

    container = Container(
        settings=settings, vault=vault, guard=guard, credentials=credentials,
        gateway=media_gateway, sessions=broker, prober=prober, monitor=monitor,
        events=events, exports=exports, snapshots=snapshots, ptz=ptz,
        storage_monitor=storage_monitor, notifications=notifications,
        federation=federation, health=HealthRegistry(), supervisor=Supervisor(),
        limiters=limiters, entitlements=resolve_entitlements(settings))

    # Wire the event pipeline's side effects now that everything exists.
    async def trigger_recording(camera_id: str, seconds: int) -> None:
        from app.db.session import session_scope
        from app.recording.triggers import trigger_recording as start

        with session_scope() as session:
            start(session, camera_id, seconds=seconds, reason="event")

    async def capture_snapshot(camera_id: str, event_id: str) -> None:
        from app.db.models import Camera
        from app.db.session import session_scope

        with session_scope() as session:
            camera = session.get(Camera, camera_id)
            if camera is not None:
                await snapshots.capture_live(session, camera, actor_id="system",
                                             event_id=event_id)

    async def notify(channels: list[str], payload: dict) -> None:
        from app.db.session import session_scope

        with session_scope() as session:
            notifications.enqueue(session, channels, payload)

    events.recording_trigger = trigger_recording
    events.snapshot_service = capture_snapshot
    events.notifier = notify

    log.info("container.built", edition=container.entitlements.edition.value,
             gateway=type(media_gateway).__name__)
    return container
