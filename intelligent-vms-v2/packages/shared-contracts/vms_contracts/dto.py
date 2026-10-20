"""Cross-service data transfer objects (the wire types).

Deliberately decoupled from the ORM models so database refactoring cannot
silently break the public API.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import (
    AlarmState, AnalyticsClass, AnalyticsSource, CameraState, DeviceProtocol,
    Edition, EventSeverity, EventType, ExportFormat, ExportState, HealthStatus,
    Permission, PtzMoveKind, RecordingMode, RecordingState, ServiceState,
    StorageState, StreamRole, TransportProtocol, VolumeKind,
)

__all__ = [
    "Problem", "Page", "TokenPair", "LoginRequest", "UserOut", "RoleOut",
    "SiteOut", "StreamOut", "CameraOut", "CameraCreate", "CameraUpdate",
    "DiscoveredDevice", "CameraCapabilities", "LiveSessionOut", "SegmentOut",
    "TimelineBucket", "PlaybackManifest", "ExportOut", "ExportCreate",
    "EvidenceRecord", "PtzCommand", "PtzPresetOut", "EventOut", "AlarmOut",
    "AlarmAck", "AnalyticsEventOut", "VolumeOut", "HealthReport",
    "ComponentHealth", "CapacityEstimate", "LicenseStatus",
]


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


# --------------------------------------------------------------------------
# Generic envelopes
# --------------------------------------------------------------------------
class Problem(_Base):
    """RFC 7807-style error body used by every endpoint.

    `code` is a stable machine-readable token; clients must branch on `code`,
    never on `detail` text.
    """

    type: str = "about:blank"
    title: str
    status: int
    code: str
    detail: str | None = None
    correlation_id: str | None = None


class Page(_Base):
    total: int
    limit: int
    offset: int
    items: list[Any]


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
class LoginRequest(_Base):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class TokenPair(_Base):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    mfa_required: bool = False


class RoleOut(_Base):
    id: str
    name: str
    builtin: bool
    permissions: list[Permission]
    description: str | None = None


class UserOut(_Base):
    id: str
    username: str
    display_name: str | None = None
    email: str | None = None
    is_active: bool
    is_locked: bool
    mfa_enabled: bool
    roles: list[str]
    site_ids: list[str]
    camera_ids: list[str]
    last_login_at: dt.datetime | None = None
    created_at: dt.datetime


# --------------------------------------------------------------------------
# Sites and cameras
# --------------------------------------------------------------------------
class SiteOut(_Base):
    id: str
    name: str
    parent_id: str | None = None
    kind: str
    path: str
    timezone: str
    camera_count: int = 0


class StreamOut(_Base):
    id: str
    role: StreamRole
    enabled: bool
    transport: TransportProtocol
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: int | None = None
    # NOTE: the source URL is intentionally ABSENT from this DTO. It contains
    # camera credentials and is never sent to a client. Verified by
    # tests/integration/test_api_flows.py::test_camera_crud_and_credential_protection


class CameraCapabilities(_Base):
    """Capability flags. False means 'not proven', never 'assumed'."""

    onvif_device: bool = False
    onvif_media: bool = False
    onvif_media2: bool = False
    onvif_ptz: bool = False
    onvif_events: bool = False
    onvif_analytics: bool = False
    onvif_recording_g: bool = False
    profile_s: bool = False
    profile_t: bool = False
    profile_g: bool = False
    profile_m: bool = False
    ptz_continuous: bool = False
    ptz_absolute: bool = False
    ptz_relative: bool = False
    ptz_presets: bool = False
    audio: bool = False
    probed_at: dt.datetime | None = None


class CameraOut(_Base):
    id: str
    name: str
    site_id: str | None
    host: str
    port: int
    protocol: DeviceProtocol
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    serial: str | None = None
    state: CameraState
    enabled: bool
    recording_mode: RecordingMode
    recording_state: RecordingState
    capabilities: CameraCapabilities
    streams: list[StreamOut] = []
    tags: list[str] = []
    last_seen_at: dt.datetime | None = None
    created_at: dt.datetime


class CameraCreate(_Base):
    name: str = Field(min_length=1, max_length=200)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    protocol: DeviceProtocol = DeviceProtocol.ONVIF
    username: str | None = None
    password: str | None = None
    site_id: str | None = None
    rtsp_main_path: str | None = None
    rtsp_sub_path: str | None = None
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    transport: TransportProtocol = TransportProtocol.RTSP_TCP
    recording_mode: RecordingMode = RecordingMode.CONTINUOUS
    storage_volume_id: str | None = None
    tags: list[str] = []

    @field_validator("host")
    @classmethod
    def _no_scheme(cls, v: str) -> str:
        if "://" in v:
            raise ValueError("host must be a bare hostname or IP, not a URL")
        return v.strip()


class CameraUpdate(_Base):
    name: str | None = None
    site_id: str | None = None
    enabled: bool | None = None
    recording_mode: RecordingMode | None = None
    username: str | None = None
    password: str | None = None
    storage_volume_id: str | None = None
    tags: list[str] | None = None


class DiscoveredDevice(_Base):
    address: str
    port: int
    xaddrs: list[str] = []
    scopes: list[str] = []
    vendor: str | None = None
    model: str | None = None
    already_registered: bool = False
    camera_id: str | None = None


# --------------------------------------------------------------------------
# Live media
# --------------------------------------------------------------------------
class LiveSessionOut(_Base):
    """What a client receives to start playing a live stream.

    `whep_url` / `hls_url` point at the media gateway, authenticated by the
    short-lived `play_token`. The camera's real RTSP URL and credentials are
    never included.
    """

    session_id: str
    camera_id: str
    stream_role: StreamRole
    path: str
    whep_url: str | None = None
    hls_url: str | None = None
    play_token: str
    expires_at: dt.datetime
    estimated_kbps: int | None = None


# --------------------------------------------------------------------------
# Recording / playback
# --------------------------------------------------------------------------
class SegmentOut(_Base):
    id: str
    camera_id: str
    stream_role: StreamRole
    started_at: dt.datetime
    ended_at: dt.datetime | None
    duration_s: float | None
    size_bytes: int | None
    state: str
    has_gap_before: bool = False


class TimelineBucket(_Base):
    start: dt.datetime
    end: dt.datetime
    recorded_s: float
    event_count: int = 0
    has_gap: bool = False


class PlaybackManifest(_Base):
    camera_id: str
    requested_from: dt.datetime
    requested_to: dt.datetime
    available_from: dt.datetime | None
    available_to: dt.datetime | None
    segments: list[SegmentOut]
    gaps: list[dict[str, Any]]
    play_token: str | None = None
    message: str | None = None


# --------------------------------------------------------------------------
# Export / evidence
# --------------------------------------------------------------------------
class ExportCreate(_Base):
    camera_id: str
    start: dt.datetime
    end: dt.datetime
    fmt: ExportFormat = ExportFormat.MP4
    reason: str | None = Field(default=None, max_length=512)
    case_reference: str | None = Field(default=None, max_length=128)

    @field_validator("end")
    @classmethod
    def _ordered(cls, v: dt.datetime, info):
        start = info.data.get("start")
        if start and v <= start:
            raise ValueError("end must be after start")
        return v


class EvidenceRecord(_Base):
    sha256: str
    size_bytes: int
    algorithm: Literal["sha256"] = "sha256"
    created_at: dt.datetime
    created_by: str
    source_camera_id: str
    source_start: dt.datetime
    source_end: dt.datetime
    actual_start: dt.datetime | None = None
    actual_end: dt.datetime | None = None
    segment_ids: list[str] = []
    chain_of_custody: list[dict[str, Any]] = []


class ExportOut(_Base):
    id: str
    camera_id: str
    state: ExportState
    fmt: ExportFormat
    requested_start: dt.datetime
    requested_end: dt.datetime
    progress: float = 0.0
    size_bytes: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    evidence: EvidenceRecord | None = None
    expires_at: dt.datetime | None = None
    created_at: dt.datetime


# --------------------------------------------------------------------------
# PTZ
# --------------------------------------------------------------------------
class PtzCommand(_Base):
    kind: PtzMoveKind
    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)
    speed: float = Field(default=0.5, gt=0.0, le=1.0)
    preset_id: str | None = None
    # Server clamps this to settings.ptz_max_move_seconds regardless of request.
    duration_s: float | None = Field(default=None, gt=0.0, le=60.0)


class PtzPresetOut(_Base):
    id: str
    camera_id: str
    name: str
    token: str | None = None
    created_at: dt.datetime


# --------------------------------------------------------------------------
# Events / alarms
# --------------------------------------------------------------------------
class EventOut(_Base):
    id: str
    type: EventType
    severity: EventSeverity
    camera_id: str | None
    site_id: str | None
    source: str
    occurred_at: dt.datetime
    received_at: dt.datetime
    payload: dict[str, Any] = {}
    dedupe_key: str | None = None
    snapshot_id: str | None = None


class AlarmOut(_Base):
    id: str
    rule_id: str | None
    state: AlarmState
    severity: EventSeverity
    camera_id: str | None
    site_id: str | None
    title: str
    first_event_at: dt.datetime
    last_event_at: dt.datetime
    event_count: int
    assigned_to: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None
    closed_at: dt.datetime | None = None
    note: str | None = None
    version: int
    escalation_level: int = 0


class AlarmAck(_Base):
    note: str | None = Field(default=None, max_length=1000)
    # Optimistic concurrency: the client echoes the version it saw. A stale
    # version is rejected with 409 rather than overwriting newer state.
    version: int


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------
class AnalyticsEventOut(_Base):
    id: str
    camera_id: str
    source: AnalyticsSource
    object_class: AnalyticsClass
    confidence: float | None
    occurred_at: dt.datetime
    bbox: list[float] | None = None
    track_id: str | None = None
    zone_id: str | None = None
    rule: str | None = None
    attributes: dict[str, Any] = {}
    plate_text: str | None = None
    has_recording: bool = False


# --------------------------------------------------------------------------
# Storage / health / capacity / licensing
# --------------------------------------------------------------------------
class VolumeOut(_Base):
    id: str
    name: str
    path: str
    kind: VolumeKind
    state: StorageState
    total_bytes: int | None = None
    free_bytes: int | None = None
    used_bytes: int | None = None
    min_free_percent: float
    enabled: bool
    camera_count: int = 0
    raid_status: str | None = None
    last_checked_at: dt.datetime | None = None


class ComponentHealth(_Base):
    name: str
    status: HealthStatus
    state: ServiceState | None = None
    detail: str | None = None
    last_ok_at: dt.datetime | None = None
    metrics: dict[str, float] = {}


class HealthReport(_Base):
    status: HealthStatus
    version: str
    uptime_s: float
    checked_at: dt.datetime
    components: list[ComponentHealth]


class CapacityEstimate(_Base):
    cameras: int
    total_ingest_mbps: float
    storage_bytes_per_day: int
    storage_bytes_for_retention: int
    retention_days: int
    cpu_cores_recommended: int
    ram_gb_recommended: int
    nic_gbps_recommended: float
    assumptions: dict[str, Any]
    disclaimer: str = (
        "Estimates are produced by an analytical model from the supplied "
        "parameters. They are NOT measured benchmarks and must be validated on "
        "the target hardware before commitment."
    )


class LicenseStatus(_Base):
    valid: bool
    edition: Edition
    licensed_cameras: int
    cameras_in_use: int
    features: list[str]
    expires_at: dt.datetime | None
    customer: str | None
    reason: str | None = None
    offline: bool = True
