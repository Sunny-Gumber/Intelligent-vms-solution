"""Application lifecycle: startup sequence, background tasks, shutdown.

Startup order is explicit and matters:

  1. configure logging
  2. ensure directories and load/create the master key
  3. wait for the database (bounded retries)
  4. seed built-in roles, the bootstrap administrator, default rules, storage
  5. run crash recovery (writing segments, stale recorder tasks, PTZ stop-all)
  6. register health probes
  7. start supervised background loops

Shutdown is the reverse and is bounded: loops are asked to stop, then
cancelled after a timeout, media sessions are released and the database pool
is disposed. A stuck task can never prevent the service from stopping.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys

from sqlalchemy import select
from vms_contracts.dto import ComponentHealth
from vms_contracts.enums import HealthStatus, ServiceState

from app.config import Settings
from app.container import Container
from app.core.backoff import BackoffPolicy
from app.core.rbac import BUILTIN_ROLES
from app.core.security import hash_password, purge_expired_revocations
from app.core.supervisor import interruptible_sleep
from app.core.timeutil import utcnow
from app.db import session as db
from app.db.models import AlarmRule, Role, StorageVolume, User, UserRole
from app.db.session import session_scope
from app.events_engine import alarms as alarm_service
from app.events_engine import escalation
from app.events_engine.rules import default_rules
from app.exports.cleanup import cleanup_expired
from app.health.registry import degraded, healthy, unhealthy
from app.logging_setup import configure_logging, get_logger
from app.recording import recovery, retention
from app.storage import volumes as volume_service

log = get_logger("lifecycle")


# --------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------
def seed_roles(session) -> None:  # noqa: ANN001
    for name, permissions in BUILTIN_ROLES.items():
        role = session.execute(
            select(Role).where(Role.name == name)).scalar_one_or_none()
        if role is None:
            session.add(Role(name=name, builtin=True,
                             permissions=[p.value for p in permissions],
                             description=f"Built-in {name} role"))
        else:
            role.builtin = True
            role.permissions = [p.value for p in permissions]


def seed_admin(session, settings: Settings) -> str | None:
    """Create the first administrator if no user exists.

    A generated password is written to <data_root>/first-run-credentials.txt
    with restrictive permissions, and the account must change it at first
    login. The password is NEVER logged or printed to stdout.
    """
    if session.execute(select(User).limit(1)).scalar_one_or_none() is not None:
        return None

    password = settings.bootstrap_admin_password or (secrets.token_urlsafe(18) + "Aa1!")
    user = User(username=settings.bootstrap_admin_username,
                display_name="Initial administrator",
                password_hash=hash_password(password),
                must_change_password=True, password_changed_at=utcnow())
    session.add(user)
    session.flush()
    role = session.execute(
        select(Role).where(Role.name == "administrator")).scalar_one()
    session.add(UserRole(user_id=user.id, role_id=role.id))

    if not settings.bootstrap_admin_password:
        target = settings.data_root / "first-run-credentials.txt"
        target.write_text(
            "Intelligent VMS first-run administrator\n"
            f"username: {user.username}\n"
            f"password: {password}\n\n"
            "Sign in and change this password immediately, then delete this file.\n",
            encoding="utf-8")
        if not sys.platform.startswith("win"):
            os.chmod(target, 0o600)
        log.warning("bootstrap.admin_created", username=user.username,
                    credentials_file=str(target))
    return user.id


def seed_default_rules(session) -> int:  # noqa: ANN001
    created = 0
    for spec in default_rules():
        exists = session.execute(
            select(AlarmRule).where(AlarmRule.name == spec["name"])).scalar_one_or_none()
        if exists is None:
            session.add(AlarmRule(**spec))
            created += 1
    return created


def seed_default_storage(session, settings: Settings) -> None:  # noqa: ANN001
    if session.execute(select(StorageVolume).limit(1)).scalar_one_or_none() is not None:
        return
    try:
        volume_service.create_volume(
            session, name="primary", path=str(settings.recordings_dir),
            min_free_percent=settings.retention_min_free_percent)
        log.info("bootstrap.storage_volume_created", path=str(settings.recordings_dir))
    except ValueError as exc:
        log.error("bootstrap.storage_volume_failed", error=str(exc))


# --------------------------------------------------------------------------
# Startup / shutdown
# --------------------------------------------------------------------------
async def startup(container: Container) -> None:
    settings = container.settings
    configure_logging(settings.log_level, settings.log_json,
                      settings.log_file or (settings.logs_dir / "control-api.log"),
                      settings.log_max_bytes, settings.log_backup_count)
    log.info("startup.begin", environment=settings.environment,
             database=("sqlite" if settings.is_sqlite else "postgresql"))

    db.wait_for_database(settings, BackoffPolicy(
        initial_s=0.5, max_s=10.0, max_attempts=settings.database_connect_retries))

    media_problems = settings.media_auth_problems()
    if media_problems:
        if settings.environment == "production":
            raise RuntimeError("Refusing to start: media admission is not configured: "
                               + "; ".join(media_problems))
        # Outside production the API still starts, but the admission hook
        # denies every viewer and every gateway-API call until fixed.
        log.error("startup.media_auth_unconfigured", problems=media_problems,
                  effect="all live video and gateway administration denied")

    with session_scope() as session:
        seed_roles(session)
    with session_scope() as session:
        seed_admin(session, settings)
        seed_default_rules(session)
        seed_default_storage(session, settings)

    with session_scope() as session:
        report = recovery.run_startup_recovery(session)
        stop_cameras = container.ptz.arbiter.stop_all_on_startup(session)
    if report.writing_found or report.orphans_found:
        log.warning("startup.recovery", writing=report.writing_found,
                    finalized=report.finalized, corrupt=report.marked_corrupt,
                    orphans=report.orphans_found,
                    tasks_released=report.tasks_released)
    if stop_cameras:
        with session_scope() as session:
            await container.ptz.stop_cameras(session, stop_cameras)

    register_health_probes(container)
    register_background_tasks(container)
    await container.supervisor.start_all()
    log.info("startup.complete")


async def shutdown(container: Container) -> None:
    log.info("shutdown.begin")
    stop_event = container.extras.get("stop_event")
    if stop_event is not None:
        stop_event.set()
    container.events.stop()
    container.monitor.stop()
    container.storage_monitor.stop()
    await container.supervisor.stop_all(timeout_s=15.0)

    try:
        with session_scope() as session:
            await container.sessions.reap_expired(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("shutdown.session_cleanup_failed", error=str(exc))

    db.dispose_engine()
    log.info("shutdown.complete")


# --------------------------------------------------------------------------
# Health probes
# --------------------------------------------------------------------------
def register_health_probes(container: Container) -> None:
    async def database_probe() -> ComponentHealth:
        ok, detail = db.healthcheck()
        return healthy("database") if ok else unhealthy("database", detail)

    async def gateway_probe() -> ComponentHealth:
        if await container.gateway.health():
            return healthy("media_gateway")
        return degraded(
            "media_gateway",
            "Live streaming is unavailable. Recording and playback continue.")

    async def storage_probe() -> ComponentHealth:
        with session_scope() as session:
            usable = volume_service.writable_volumes(session)
            total = session.execute(select(StorageVolume)).scalars().all()
        if not total:
            return degraded("storage", "no storage volume is configured")
        if not usable:
            return unhealthy("storage",
                             "no writable storage volume; recording will fail")
        free = min((v.free_bytes or 0) / (v.total_bytes or 1) * 100 for v in usable)
        if free < container.settings.storage_critical_free_percent:
            return unhealthy("storage", f"only {free:.1f}% free on the best volume")
        return healthy("storage", f"{len(usable)} writable volume(s)",
                       free_percent=free)

    async def events_probe() -> ComponentHealth:
        stats = container.events.stats()
        numeric = {k: float(v) for k, v in stats.items()}
        if stats["dropped"] > 0:
            return degraded("events",
                            f"{stats['dropped']} events dropped (queue pressure)",
                            **numeric)
        return healthy("events", **numeric)

    async def workers_probe() -> ComponentHealth:
        statuses = container.supervisor.statuses()
        failing = [s.name for s in statuses if s.critical and not s.healthy]
        if failing:
            return unhealthy("background_workers",
                             f"not running: {', '.join(failing)}")
        return healthy("background_workers", f"{len(statuses)} task(s)")

    async def license_probe() -> ComponentHealth:
        entitlements = container.entitlements
        if entitlements.valid:
            return healthy("license", f"{entitlements.edition.value} edition")
        return ComponentHealth(name="license", status=HealthStatus.DEGRADED,
                               state=ServiceState.RUNNING,
                               detail=entitlements.reason or "running in limited mode")

    container.health.register("database", database_probe, critical=True)
    container.health.register("storage", storage_probe, critical=True)
    container.health.register("media_gateway", gateway_probe)
    container.health.register("events", events_probe)
    container.health.register("background_workers", workers_probe)
    container.health.register("license", license_probe)


# --------------------------------------------------------------------------
# Background tasks
# --------------------------------------------------------------------------
def register_background_tasks(container: Container) -> None:
    settings = container.settings
    supervisor = container.supervisor
    stop = asyncio.Event()
    container.extras["stop_event"] = stop

    supervisor.register("event-pipeline", container.events.run, critical=True)
    supervisor.register("device-monitor", container.monitor.run)
    supervisor.register("storage-monitor", container.storage_monitor.run)

    async def retention_loop() -> None:
        while not stop.is_set():
            if await interruptible_sleep(stop, settings.retention_scan_interval_s):
                return
            with session_scope() as session:
                result = retention.run_retention(session, settings=settings)
            if result.deleted:
                log.info("retention.cycle", deleted=result.deleted,
                         bytes=result.bytes_reclaimed)

    async def session_reaper() -> None:
        while not stop.is_set():
            if await interruptible_sleep(stop, 30):
                return
            with session_scope() as session:
                await container.sessions.reap_expired(session)
                container.ptz.arbiter.expire_locks(session)

    async def ptz_watchdog() -> None:
        """Stops cameras whose movement window elapsed or whose client vanished."""
        while not stop.is_set():
            if await interruptible_sleep(stop, settings.ptz_deadman_interval_s):
                return
            with session_scope() as session:
                camera_ids = container.ptz.arbiter.cameras_needing_stop(session)
                if camera_ids:
                    await container.ptz.stop_cameras(session, camera_ids)

    async def maintenance_loop() -> None:
        while not stop.is_set():
            if await interruptible_sleep(stop, 300):
                return
            with session_scope() as session:
                purge_expired_revocations(session)
                cleanup_expired(session, settings)
                alarm_service.auto_close_stale(
                    session, hours=settings.alarm_auto_close_hours)
                container.events.dedupe.purge_expired()

    async def escalation_loop() -> None:
        while not stop.is_set():
            if await interruptible_sleep(stop, settings.escalation_interval_s):
                return
            with session_scope() as session:
                for item in escalation.due_escalations(session):
                    container.notifications.enqueue(
                        session, item.channels,
                        {"alarm_id": item.alarm_id, "title": item.title,
                         "severity": "high", "escalation_level": item.level})
                    escalation.mark_escalated(session, item.alarm_id, item.level)

    async def notification_loop() -> None:
        while not stop.is_set():
            if await interruptible_sleep(stop, 10):
                return
            with session_scope() as session:
                await container.notifications.process_pending(session)

    async def media_revocation_loop() -> None:
        """Disconnect gateway sessions whose authorization has ended.

        Covers closed/expired sessions, logout, deactivation, permission
        changes and superseded reconnects, including those decided by
        synchronous code paths. State is in the database, so the first cycle
        after a restart resumes outstanding revocations.
        """
        while not stop.is_set():
            if await interruptible_sleep(stop, settings.media_revocation_interval_s):
                return
            with session_scope() as session:
                result = await container.sessions.connections.enforce(session)
            if result.kicked or result.kick_failures or result.abandoned:
                log.info("media.revocation_cycle", kicked=result.kicked,
                         failures=result.kick_failures, abandoned=result.abandoned,
                         reasons=result.reasons)
            supervisor.heartbeat("media-revocation")

    supervisor.register("retention", retention_loop)
    supervisor.register("media-revocation", media_revocation_loop, critical=True)
    supervisor.register("session-reaper", session_reaper, critical=True)
    supervisor.register("ptz-watchdog", ptz_watchdog, critical=True)
    supervisor.register("maintenance", maintenance_loop)
    supervisor.register("escalation", escalation_loop)
    supervisor.register("notifications", notification_loop)

    if settings.federation_enabled:
        async def federation_loop() -> None:
            while not stop.is_set():
                if await interruptible_sleep(stop, settings.federation_poll_interval_s):
                    return
                with session_scope() as session:
                    await container.federation.poll_all(session)

        supervisor.register("federation", federation_loop)
