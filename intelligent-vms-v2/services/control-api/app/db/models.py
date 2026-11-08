"""ORM models — the persistent state of Intelligent VMS.

Design rules applied throughout:
  * Camera secrets live only in CameraCredential.secret_blob (AES-256-GCM),
    never in a plain column and never in a DTO.
  * Objects multiple actors can change concurrently (alarms, exports, PTZ
    locks, cameras) carry a `version` column for optimistic concurrency.
  * Deleting a camera cascades to its configuration rows but NOT to recorded
    segments or audit rows: evidence must survive device removal. That is why
    RecordingSegment.camera_id is a plain String, not a ForeignKey.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.timeutil import utcnow
from app.db.base import Base, IdMixin, JSONType, TimestampMixin, UtcDateTime, new_id

# ==========================================================================
# Identity and access
# ==========================================================================


class Role(Base, IdMixin, TimestampMixin):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    permissions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)

    users: Mapped[list[UserRole]] = relationship(back_populates="role",
                                                   cascade="all, delete-orphan")


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    password_hash: Mapped[str | None] = mapped_column(String(512))
    external_subject: Mapped[str | None] = mapped_column(String(256), index=True)
    identity_provider: Mapped[str] = mapped_column(String(32), default="local", nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    locked_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret_blob: Mapped[str | None] = mapped_column(Text)

    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    preferences: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    roles: Mapped[list[UserRole]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin")
    site_scopes: Mapped[list[UserSiteScope]] = relationship(
        cascade="all, delete-orphan", lazy="selectin")
    camera_scopes: Mapped[list[UserCameraScope]] = relationship(
        cascade="all, delete-orphan", lazy="selectin")


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[str] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)

    user: Mapped[User] = relationship(back_populates="roles")
    role: Mapped[Role] = relationship(back_populates="users", lazy="selectin")


class UserSiteScope(Base):
    """Restricts a user to specific sites. No rows == all sites (admins only)."""

    __tablename__ = "user_site_scopes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)


class UserCameraScope(Base):
    """Per-camera grant or deny, layered on top of site scope."""

    __tablename__ = "user_camera_scopes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True)
    deny: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RefreshToken(Base, IdMixin):
    """Server-side refresh record enabling true revocation and rotation."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(String(32), default=new_id, index=True,
                                           nullable=False)
    issued_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    replaced_by: Mapped[str | None] = mapped_column(String(32))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    client_ip: Mapped[str | None] = mapped_column(String(64))


class RevokedAccessToken(Base):
    """Denylist of access-token JTIs revoked before natural expiry."""

    __tablename__ = "revoked_access_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    revoked_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128))


class ApiKey(Base, IdMixin, TimestampMixin):
    """Service-account credential for third-party integrations (Module 19)."""

    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    permissions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=600, nullable=False)


class LoginAttempt(Base, IdMixin):
    __tablename__ = "login_attempts"

    username: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False,
                                            index=True)


class AuditEntry(Base, IdMixin):
    """Append-only audit trail. The application exposes no update path."""

    __tablename__ = "audit_log"

    at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False,
                                            index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), index=True)
    actor_name: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(96), index=True, nullable=False)
    object_type: Mapped[str | None] = mapped_column(String(48), index=True)
    object_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(16), default="success", nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(32), index=True)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


# ==========================================================================
# Sites and devices
# ==========================================================================


class Site(Base, IdMixin, TimestampMixin):
    """Hierarchy node: organization > site > building > floor > zone."""

    __tablename__ = "sites"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="site", nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(1024), default="/", nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    address: Mapped[str | None] = mapped_column(String(512))
    node_id: Mapped[str | None] = mapped_column(String(32), index=True)  # federation owner
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("parent_id", "name", name="site_unique_under_parent"),)


class Camera(Base, IdMixin, TimestampMixin):
    __tablename__ = "cameras"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    site_id: Mapped[str | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), index=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    rtsp_port: Mapped[int] = mapped_column(Integer, default=554, nullable=False)
    protocol: Mapped[str] = mapped_column(String(24), default="onvif", nullable=False)
    transport: Mapped[str] = mapped_column(String(16), default="rtsp_tcp", nullable=False)

    vendor: Mapped[str | None] = mapped_column(String(96))
    model: Mapped[str | None] = mapped_column(String(96))
    firmware: Mapped[str | None] = mapped_column(String(96))
    serial: Mapped[str | None] = mapped_column(String(96), index=True)
    mac: Mapped[str | None] = mapped_column(String(32), index=True)
    adapter: Mapped[str] = mapped_column(String(48), default="generic", nullable=False)

    state: Mapped[str] = mapped_column(String(24), default="provisioning", nullable=False,
                                       index=True)
    state_detail: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    capabilities: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)

    recording_mode: Mapped[str] = mapped_column(String(24), default="continuous",
                                                nullable=False)
    recording_state: Mapped[str] = mapped_column(String(24), default="idle", nullable=False,
                                                 index=True)
    recording_detail: Mapped[str | None] = mapped_column(String(512))
    recording_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("recording_profiles.id", ondelete="SET NULL"))
    storage_volume_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_volumes.id", ondelete="SET NULL"), index=True)
    retention_days: Mapped[int | None] = mapped_column(Integer)

    analytics_config: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    streams: Mapped[list[CameraStream]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="selectin")
    credential: Mapped[CameraCredential | None] = relationship(
        back_populates="camera", cascade="all, delete-orphan", uselist=False,
        lazy="selectin")

    __table_args__ = (
        UniqueConstraint("host", "port", "serial", name="camera_unique_endpoint"),
        Index("ix_cameras_site_state", "site_id", "state"),
    )


class CameraStream(Base, IdMixin, TimestampMixin):
    __tablename__ = "camera_streams"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # May embed credentials; stored encrypted when it does.
    source_url: Mapped[str | None] = mapped_column(Text)
    source_url_encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    onvif_profile_token: Mapped[str | None] = mapped_column(String(128))
    transport: Mapped[str] = mapped_column(String(16), default="rtsp_tcp", nullable=False)
    codec: Mapped[str | None] = mapped_column(String(24))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    audio_codec: Mapped[str | None] = mapped_column(String(24))
    record: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    probed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    camera: Mapped[Camera] = relationship(back_populates="streams")

    __table_args__ = (UniqueConstraint("camera_id", "role", name="stream_unique_role"),)


class CameraCredential(Base, IdMixin, TimestampMixin):
    __tablename__ = "camera_credentials"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(128))
    secret_blob: Mapped[str | None] = mapped_column(Text)   # AES-256-GCM
    key_id: Mapped[str | None] = mapped_column(String(16))
    rotated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    camera: Mapped[Camera] = relationship(back_populates="credential")


class PtzPreset(Base, IdMixin, TimestampMixin):
    __tablename__ = "ptz_presets"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token: Mapped[str | None] = mapped_column(String(128))
    position: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (UniqueConstraint("camera_id", "name", name="preset_unique_name"),)


class PtzLock(Base):
    """Control arbitration. One row per camera currently being driven."""

    __tablename__ = "ptz_locks"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(128))
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    acquired_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                     nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    moving_until: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# ==========================================================================
# Storage and recording
# ==========================================================================


class StorageVolume(Base, IdMixin, TimestampMixin):
    __tablename__ = "storage_volumes"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="local", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="online", nullable=False)
    state_detail: Mapped[str | None] = mapped_column(String(512))
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    min_free_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    quota_bytes: Mapped[int | None] = mapped_column(BigInteger)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    free_bytes: Mapped[int | None] = mapped_column(BigInteger)
    used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    raid_device: Mapped[str | None] = mapped_column(String(128))
    raid_status: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)

    __table_args__ = (CheckConstraint("min_free_percent >= 0", name="min_free_nonneg"),)


class RecordingProfile(Base, IdMixin, TimestampMixin):
    __tablename__ = "recording_profiles"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    stream_role: Mapped[str] = mapped_column(String(16), default="main", nullable=False)
    segment_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, default=14, nullable=False)
    record_audio: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    pre_event_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    post_event_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    transcode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))


class RecordingSchedule(Base, IdMixin, TimestampMixin):
    """Weekly schedule. `blocks` is a list of {day, start, end} in SITE time."""

    __tablename__ = "recording_schedules"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), default="default", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(24), default="continuous", nullable=False)
    blocks: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    __table_args__ = (UniqueConstraint("camera_id", "name", name="schedule_unique_name"),)


class RecordingSegment(Base, IdMixin):
    """One finalized media file plus its index entry.

    camera_id is deliberately a plain column, not a ForeignKey: recorded
    evidence must survive deletion of the camera it came from.
    """

    __tablename__ = "recording_segments"

    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    camera_name: Mapped[str | None] = mapped_column(String(200))
    stream_role: Mapped[str] = mapped_column(String(16), default="main", nullable=False)
    volume_id: Mapped[str | None] = mapped_column(String(32), index=True)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    duration_s: Mapped[float | None] = mapped_column(Float)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    codec: Mapped[str | None] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(16), default="writing", nullable=False,
                                       index=True)
    recording_reason: Mapped[str] = mapped_column(String(24), default="continuous",
                                                  nullable=False)
    has_gap_before: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                    nullable=False)

    __table_args__ = (
        Index("ix_segment_camera_time", "camera_id", "started_at", "ended_at"),
        UniqueConstraint("volume_id", "relative_path", name="segment_unique_path"),
    )


class RecordingTask(Base, IdMixin, TimestampMixin):
    """Desired-state record consumed by the recording service workers."""

    __tablename__ = "recording_tasks"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False)
    stream_role: Mapped[str] = mapped_column(String(16), default="main", nullable=False)
    reason: Mapped[str] = mapped_column(String(24), default="continuous", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(32))
    starts_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    ends_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), index=True)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    state: Mapped[str] = mapped_column(String(16), default="idle", nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(512))
    restart_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class RecordingGap(Base, IdMixin):
    __tablename__ = "recording_gaps"

    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    stream_role: Mapped[str] = mapped_column(String(16), default="main", nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    ended_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)
    cause: Mapped[str | None] = mapped_column(String(128))
    detected_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                     nullable=False)


class Snapshot(Base, IdMixin):
    __tablename__ = "snapshots"

    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    taken_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                  nullable=False, index=True)
    taken_by: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(32), index=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))


# ==========================================================================
# Exports and evidence
# ==========================================================================


class ExportJob(Base, IdMixin, TimestampMixin):
    __tablename__ = "export_jobs"

    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    requested_start: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    requested_end: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    fmt: Mapped[str] = mapped_column(String(16), default="mp4", nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="queued", nullable=False,
                                       index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(1024))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    actual_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    actual_end: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    segment_ids: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(512))
    case_reference: Mapped[str | None] = mapped_column(String(128))
    chain_of_custody: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(String(1024))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    download_token_hash: Mapped[str | None] = mapped_column(String(128))
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# ==========================================================================
# Events, alarms, notifications
# ==========================================================================


class Event(Base, IdMixin):
    __tablename__ = "events"

    type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False,
                                          index=True)
    camera_id: Mapped[str | None] = mapped_column(String(32), index=True)
    site_id: Mapped[str | None] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32), default="system", nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    received_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                     nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), index=True)
    alarm_id: Mapped[str | None] = mapped_column(String(32), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (Index("ix_events_type_time", "type", "occurred_at"),)


class AlarmRule(Base, IdMixin, TimestampMixin):
    """User-defined rule: match events -> raise alarm / record / notify."""

    __tablename__ = "alarm_rules"

    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    event_types: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    camera_ids: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    site_ids: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    conditions: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    raise_alarm: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    start_recording: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    record_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    capture_snapshot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_channels: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    suppress_seconds: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    escalate_after_s: Mapped[int | None] = mapped_column(Integer)
    escalate_channels: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    schedule: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class Alarm(Base, IdMixin, TimestampMixin):
    __tablename__ = "alarms"

    rule_id: Mapped[str | None] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(16), default="new", nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False,
                                          index=True)
    camera_id: Mapped[str | None] = mapped_column(String(32), index=True)
    site_id: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), index=True)
    first_event_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False,
                                                        index=True)
    last_event_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    assigned_to: Mapped[str | None] = mapped_column(String(32), index=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(32))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(32))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    note: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[str | None] = mapped_column(String(32))
    escalation_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    escalated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    # Optimistic concurrency guard — see app/events_engine/alarms.py
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    __table_args__ = (Index("ix_alarms_state_time", "state", "first_event_at"),)


class NotificationChannel(Base, IdMixin, TimestampMixin):
    __tablename__ = "notification_channels"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)   # webhook | email
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    secret_blob: Mapped[str | None] = mapped_column(Text)           # HMAC key / SMTP pw
    last_success_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(String(512))
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NotificationDelivery(Base, IdMixin):
    __tablename__ = "notification_deliveries"

    channel_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    alarm_id: Mapped[str | None] = mapped_column(String(32), index=True)
    event_id: Mapped[str | None] = mapped_column(String(32), index=True)
    state: Mapped[str] = mapped_column(String(16), default="pending", nullable=False,
                                       index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(String(512))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                    nullable=False)


# ==========================================================================
# Analytics
# ==========================================================================


class AnalyticsEvent(Base, IdMixin):
    __tablename__ = "analytics_events"

    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    object_class: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    received_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                     nullable=False)
    bbox: Mapped[list | None] = mapped_column(JSONType)
    track_id: Mapped[str | None] = mapped_column(String(64), index=True)
    zone_id: Mapped[str | None] = mapped_column(String(32), index=True)
    rule: Mapped[str | None] = mapped_column(String(48), index=True)
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    plate_text: Mapped[str | None] = mapped_column(String(32), index=True)
    plate_region: Mapped[str | None] = mapped_column(String(16))
    face_subject_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_id: Mapped[str | None] = mapped_column(String(32), index=True)
    segment_id: Mapped[str | None] = mapped_column(String(32), index=True)
    model_name: Mapped[str | None] = mapped_column(String(96))
    model_version: Mapped[str | None] = mapped_column(String(32))

    __table_args__ = (
        Index("ix_analytics_cam_time_class", "camera_id", "occurred_at", "object_class"),
    )


class AnalyticsZone(Base, IdMixin, TimestampMixin):
    """Detection zone / tripwire in normalized image coordinates."""

    __tablename__ = "analytics_zones"

    camera_id: Mapped[str] = mapped_column(
        ForeignKey("cameras.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="region", nullable=False)
    polygon: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    direction: Mapped[str | None] = mapped_column(String(16))
    object_classes: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    dwell_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    schedule: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)

    __table_args__ = (UniqueConstraint("camera_id", "name", name="zone_unique_name"),)


class PrivacyAccessLog(Base, IdMixin):
    """Mandatory audit for facial recognition and ANPR data access (M9-R24).

    Kept separate from the general audit log and never purged by normal
    retention.
    """

    __tablename__ = "privacy_access_log"

    at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False,
                                            index=True)
    actor_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    capability: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    subject_reference: Mapped[str | None] = mapped_column(String(128))
    camera_id: Mapped[str | None] = mapped_column(String(32))
    justification: Mapped[str | None] = mapped_column(String(512))
    result_count: Mapped[int | None] = mapped_column(Integer)


# ==========================================================================
# Live sessions, layouts, system, federation
# ==========================================================================


class LiveSession(Base, IdMixin):
    """Bounded, expiring grant for one client to view one stream."""

    __tablename__ = "live_sessions"

    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    stream_role: Mapped[str] = mapped_column(String(16), default="main", nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    client_ip: Mapped[str | None] = mapped_column(String(64))
    estimated_kbps: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                    nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                      nullable=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    close_reason: Mapped[str | None] = mapped_column(String(64))


class MediaConnection(Base, IdMixin):
    """One gateway connection admitted for a live session (ADR-0015 s6).

    Persisted (not in memory) so that a control-API restart does not lose the
    ability to disconnect a specific viewer. A row is "open" while closed_at is
    NULL. revoke_reason is set when policy decides the connection must end;
    the revocation loop then kicks it at the gateway and sets closed_at.
    HLS reads are not bound: the gateway exposes no per-viewer HLS session.
    """

    __tablename__ = "media_connections"

    live_session_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    camera_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    protocol: Mapped[str] = mapped_column(String(16), nullable=False)
    gateway_conn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                    nullable=False)
    last_admitted_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                          nullable=False)
    last_policy_check_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    revoke_reason: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    kick_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    kicked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    close_reason: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("protocol", "gateway_conn_id", name="media_conn_unique_gateway_id"),
        Index("ix_media_conn_open_session", "live_session_id", "closed_at"),
        Index("ix_media_conn_policy_scan", "closed_at", "revoke_reason",
              "last_policy_check_at"),
    )


class Layout(Base, IdMixin, TimestampMixin):
    __tablename__ = "layouts"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(32), index=True)
    shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rows: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    cols: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    tiles: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    monitor_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("owner_id", "name", name="layout_unique_per_owner"),)


class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                    onupdate=utcnow, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(32))


class FederationNode(Base, IdMixin, TimestampMixin):
    """A registered remote Intelligent VMS server (Module 12)."""

    __tablename__ = "federation_nodes"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(128))
    token_blob: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    last_contact_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(String(512))
    summary: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)


class WorkerHeartbeat(Base):
    """Liveness of out-of-process workers (recorder, analytics, event poller)."""

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    host: Mapped[str | None] = mapped_column(String(128))
    pid: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                    nullable=False)
    heartbeat_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow,
                                                      nullable=False, index=True)
    metrics: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
