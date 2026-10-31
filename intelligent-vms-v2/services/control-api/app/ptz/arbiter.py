"""PTZ control arbitration and the dead-man safety system.

The failure this module exists to prevent: an operator presses "pan left", the
network drops, and the camera keeps panning until it hits its limit — a real,
well-known VMS defect. Three independent safeguards:

 1. Every movement command carries an explicit device-side timeout (ONVIF
    ContinuousMove Timeout), so even a dead server stops the camera.
 2. The server records `moving_until`; a watchdog sends Stop to any camera
    whose movement window has elapsed.
 3. The control lock must be refreshed by the client. If the client
    disappears, the lock expires, the watchdog stops the camera and releases
    control.

Arbitration: one controller at a time, with priority preemption. An equal or
lower priority request is refused with a message naming the current holder.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.errors import ConflictError
from app.core.timeutil import utcnow
from app.db.models import PtzLock
from app.logging_setup import get_logger

log = get_logger("ptz.arbiter")


@dataclass(frozen=True)
class LockGrant:
    camera_id: str
    owner_id: str
    expires_at: dt.datetime
    preempted: str | None = None


class PtzArbiter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def acquire(self, session: Session, camera_id: str, owner_id: str, *,
                owner_name: str | None = None, priority: int = 0) -> LockGrant:
        now = utcnow()
        expires = now + dt.timedelta(seconds=self.settings.ptz_lock_ttl_s)
        lock = session.get(PtzLock, camera_id)

        if lock is None:
            session.add(PtzLock(camera_id=camera_id, owner_id=owner_id,
                                owner_name=owner_name, priority=priority,
                                expires_at=expires))
            session.flush()
            return LockGrant(camera_id, owner_id, expires)

        if lock.owner_id == owner_id:
            lock.expires_at = expires
            lock.version += 1
            return LockGrant(camera_id, owner_id, expires)

        if lock.expires_at <= now:
            preempted = lock.owner_id
            self._assign(lock, owner_id, owner_name, priority, expires)
            return LockGrant(camera_id, owner_id, expires, preempted=preempted)

        if priority > lock.priority:
            preempted = lock.owner_name or lock.owner_id
            log.info("ptz.preempted", camera_id=camera_id, previous=lock.owner_id,
                     new=owner_id)
            self._assign(lock, owner_id, owner_name, priority, expires)
            return LockGrant(camera_id, owner_id, expires, preempted=preempted)

        raise ConflictError(
            f"{lock.owner_name or 'Another operator'} is currently controlling this camera.",
            code="ptz_locked",
            extra={"owner": lock.owner_name or lock.owner_id,
                   "expires_in_s": max(0, int((lock.expires_at - now).total_seconds()))})

    def _assign(self, lock: PtzLock, owner_id: str, owner_name: str | None,
                priority: int, expires: dt.datetime) -> None:
        lock.owner_id = owner_id
        lock.owner_name = owner_name
        lock.priority = priority
        lock.acquired_at = utcnow()
        lock.expires_at = expires
        lock.moving_until = None
        lock.version += 1

    def mark_moving(self, session: Session, camera_id: str, owner_id: str,
                    duration_s: float) -> dt.datetime:
        """Record a movement window, clamped to the configured maximum."""
        clamped = min(max(0.1, duration_s), self.settings.ptz_max_move_seconds)
        lock = session.get(PtzLock, camera_id)
        if lock is None or lock.owner_id != owner_id:
            raise ConflictError("You do not hold PTZ control of this camera",
                                code="ptz_not_owner")
        lock.moving_until = utcnow() + dt.timedelta(seconds=clamped)
        lock.expires_at = max(lock.expires_at, lock.moving_until)
        lock.version += 1
        return lock.moving_until

    def clear_moving(self, session: Session, camera_id: str) -> None:
        lock = session.get(PtzLock, camera_id)
        if lock is not None:
            lock.moving_until = None
            lock.version += 1

    def release(self, session: Session, camera_id: str,
                owner_id: str | None = None) -> bool:
        lock = session.get(PtzLock, camera_id)
        if lock is None:
            return False
        if owner_id and lock.owner_id != owner_id:
            return False
        session.delete(lock)
        return True

    def owner(self, session: Session, camera_id: str) -> PtzLock | None:
        lock = session.get(PtzLock, camera_id)
        if lock is None or lock.expires_at <= utcnow():
            return None
        return lock

    def cameras_needing_stop(self, session: Session) -> list[str]:
        """Cameras whose movement window elapsed or whose owner vanished."""
        now = utcnow()
        needing: list[str] = []
        for lock in session.execute(select(PtzLock)).scalars().all():
            if lock.moving_until is not None and lock.moving_until <= now:
                needing.append(lock.camera_id)
            elif lock.expires_at <= now:
                needing.append(lock.camera_id)
        return needing

    def expire_locks(self, session: Session) -> int:
        rows = session.execute(
            select(PtzLock).where(PtzLock.expires_at <= utcnow())).scalars().all()
        for lock in rows:
            session.delete(lock)
        if rows:
            log.info("ptz.locks_expired", count=len(rows))
        return len(rows)

    def stop_all_on_startup(self, session: Session) -> list[str]:
        """After a restart we cannot know what is moving: stop everything.

        This is the recovery path for "the server was killed while a camera
        was panning".
        """
        rows = session.execute(select(PtzLock)).scalars().all()
        camera_ids = [lock.camera_id for lock in rows]
        for lock in rows:
            session.delete(lock)
        # Flush so an immediately following owner()/query sees the cleared state;
        # the caller acts on this result straight away.
        session.flush()
        if camera_ids:
            log.warning("ptz.startup_stop_all", count=len(camera_ids))
        return camera_ids
