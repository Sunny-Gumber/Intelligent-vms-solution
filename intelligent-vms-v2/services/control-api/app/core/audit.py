"""Append-only audit trail.

Every security-relevant action records who did what, to which object, from
where, with the correlation id that ties it to request logs. Writes are
best-effort-but-loud: if the audit insert fails the request fails, because a
VMS that silently loses its audit trail is not defensible in an investigation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.db.models import AuditEntry
from app.logging_setup import correlation_id, get_logger

log = get_logger("audit")


class Action:
    """Stable action tokens. UIs and SIEM rules depend on these strings."""

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGOUT = "auth.logout"
    TOKEN_REFRESH = "auth.token.refresh"
    TOKEN_REVOKE = "auth.token.revoke"
    PASSWORD_CHANGE = "auth.password.change"
    PASSWORD_RESET = "auth.password.reset"
    MFA_ENABLE = "auth.mfa.enable"
    MFA_DISABLE = "auth.mfa.disable"

    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    ROLE_CREATE = "role.create"
    ROLE_UPDATE = "role.update"
    ROLE_DELETE = "role.delete"
    APIKEY_CREATE = "apikey.create"
    APIKEY_REVOKE = "apikey.revoke"

    CAMERA_CREATE = "camera.create"
    CAMERA_UPDATE = "camera.update"
    CAMERA_DELETE = "camera.delete"
    CAMERA_REPLACE = "camera.replace"
    CAMERA_IMPORT = "camera.import"
    CAMERA_EXPORT = "camera.export"
    CAMERA_CREDENTIAL_CHANGE = "camera.credential.change"
    DISCOVERY_RUN = "camera.discovery.run"

    LIVE_SESSION_OPEN = "live.session.open"
    LIVE_SESSION_CLOSE = "live.session.close"
    SNAPSHOT_TAKE = "snapshot.take"
    PLAYBACK_REQUEST = "playback.request"

    RECORDING_START_MANUAL = "recording.manual.start"
    RECORDING_STOP_MANUAL = "recording.manual.stop"
    SCHEDULE_UPDATE = "recording.schedule.update"
    RETENTION_DELETE = "recording.retention.delete"

    EXPORT_CREATE = "export.create"
    EXPORT_CANCEL = "export.cancel"
    EXPORT_DOWNLOAD = "export.download"
    EXPORT_DELETE = "export.delete"

    PTZ_COMMAND = "ptz.command"
    PTZ_PRESET_SAVE = "ptz.preset.save"
    PTZ_PRESET_DELETE = "ptz.preset.delete"

    ALARM_ACK = "alarm.acknowledge"
    ALARM_RESOLVE = "alarm.resolve"
    ALARM_CLOSE = "alarm.close"
    ALARM_ASSIGN = "alarm.assign"
    RULE_UPDATE = "alarm.rule.update"

    STORAGE_UPDATE = "storage.update"
    STORAGE_MIGRATE = "storage.migrate"
    SYSTEM_CONFIGURE = "system.configure"
    LICENSE_UPDATE = "license.update"
    BACKUP_CREATE = "system.backup"
    RESTORE_RUN = "system.restore"
    DIAG_BUNDLE = "system.diagnostics.bundle"
    PRIVACY_QUERY = "privacy.query"


_SENSITIVE = {"password", "secret", "token", "credential", "secret_blob", "totp_secret"}


def _scrub(detail: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if key.lower() in _SENSITIVE:
            out[key] = "***redacted***"
        elif isinstance(value, dict):
            out[key] = _scrub(value)
        else:
            out[key] = value
    return out


def record(session: Session, *, action: str, actor_id: str | None = None,
           actor_name: str | None = None, object_type: str | None = None,
           object_id: str | None = None, outcome: str = "success",
           client_ip: str | None = None,
           detail: dict[str, Any] | None = None) -> AuditEntry:
    entry = AuditEntry(
        at=utcnow(), actor_id=actor_id, actor_name=actor_name, action=action,
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        outcome=outcome, client_ip=client_ip, correlation_id=correlation_id.get(),
        detail=_scrub(detail or {}))
    session.add(entry)
    log.info("audit", action=action, actor=actor_id, object_type=object_type,
             object_id=object_id, outcome=outcome)
    return entry


def search(session: Session, *, actor_id: str | None = None, action: str | None = None,
           object_type: str | None = None, object_id: str | None = None,
           since=None, until=None, limit: int = 100, offset: int = 0):
    stmt = select(AuditEntry)
    if actor_id:
        stmt = stmt.where(AuditEntry.actor_id == actor_id)
    if action:
        stmt = stmt.where(AuditEntry.action == action)
    if object_type:
        stmt = stmt.where(AuditEntry.object_type == object_type)
    if object_id:
        stmt = stmt.where(AuditEntry.object_id == str(object_id))
    if since is not None:
        stmt = stmt.where(AuditEntry.at >= since)
    if until is not None:
        stmt = stmt.where(AuditEntry.at <= until)
    return session.execute(
        stmt.order_by(AuditEntry.at.desc()).limit(limit).offset(offset)).scalars().all()
