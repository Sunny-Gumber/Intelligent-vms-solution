"""Cross-service enumerations.

All values are lowercase snake_case strings so they are stable across
PostgreSQL storage, JSON APIs, the React client and the .NET client.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "Role", "Permission", "CameraState", "StreamRole", "TransportProtocol",
    "DeviceProtocol", "RecordingMode", "RecordingState", "SegmentState",
    "ExportState", "ExportFormat", "EventType", "EventSeverity", "AlarmState",
    "AnalyticsClass", "AnalyticsSource", "StorageState", "VolumeKind",
    "PtzMoveKind", "ServiceState", "HealthStatus", "Edition", "FeatureFlag",
    "ImplementationStatus",
]


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# --------------------------------------------------------------------------
# Identity and authorization
# --------------------------------------------------------------------------
class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    OPERATOR = "operator"
    VIEWER = "viewer"
    CUSTOM = "custom"


class Permission(StrEnum):
    """Fine-grained, server-enforced permissions.

    Client-side checks are cosmetic only; every permission here is re-validated
    in app.core.deps before the handler executes.
    """

    # Viewing
    LIVE_VIEW = "live.view"
    PLAYBACK_VIEW = "playback.view"
    SNAPSHOT_CAPTURE = "snapshot.capture"

    # Operations
    RECORDING_MANUAL = "recording.manual"
    PTZ_CONTROL = "ptz.control"
    PTZ_PRESET_MANAGE = "ptz.preset.manage"
    EXPORT_CREATE = "export.create"
    EXPORT_DOWNLOAD = "export.download"
    ALARM_ACKNOWLEDGE = "alarm.acknowledge"
    ALARM_RESOLVE = "alarm.resolve"

    # Configuration
    CAMERA_MANAGE = "camera.manage"
    SITE_MANAGE = "site.manage"
    SCHEDULE_MANAGE = "schedule.manage"
    STORAGE_MANAGE = "storage.manage"
    RULE_MANAGE = "rule.manage"
    ANALYTICS_MANAGE = "analytics.manage"

    # Sensitive / privacy-impacting
    FACE_RECOGNITION_USE = "privacy.face.use"
    ANPR_USE = "privacy.anpr.use"

    # Administration
    USER_MANAGE = "user.manage"
    ROLE_MANAGE = "role.manage"
    SYSTEM_CONFIGURE = "system.configure"
    AUDIT_READ = "audit.read"
    LICENSE_MANAGE = "license.manage"
    DIAGNOSTICS_READ = "diagnostics.read"
    API_KEY_MANAGE = "apikey.manage"
    FEDERATION_MANAGE = "federation.manage"


# --------------------------------------------------------------------------
# Devices and media
# --------------------------------------------------------------------------
class CameraState(StrEnum):
    PROVISIONING = "provisioning"
    ONLINE = "online"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    DISABLED = "disabled"
    ERROR = "error"


class StreamRole(StrEnum):
    MAIN = "main"
    SUB = "sub"
    THIRD = "third"


class TransportProtocol(StrEnum):
    RTSP_TCP = "rtsp_tcp"
    RTSP_UDP = "rtsp_udp"
    RTSPS_TCP = "rtsps_tcp"
    HTTP_MJPEG = "http_mjpeg"


class DeviceProtocol(StrEnum):
    ONVIF = "onvif"
    RTSP = "rtsp"
    VENDOR_ADAPTER = "vendor_adapter"


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
class RecordingMode(StrEnum):
    DISABLED = "disabled"
    CONTINUOUS = "continuous"
    SCHEDULED = "scheduled"
    EVENT = "event"
    MANUAL_ONLY = "manual_only"


class RecordingState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    RETRYING = "retrying"
    FAILED = "failed"
    STOPPING = "stopping"


class SegmentState(StrEnum):
    """Lifecycle of a single recorded file on disk."""

    WRITING = "writing"       # *.part on disk, not yet playable
    FINALIZED = "finalized"   # atomically renamed, indexed, playable
    CORRUPT = "corrupt"       # failed verification
    ORPHANED = "orphaned"     # on disk but not indexed (found at startup)
    DELETED = "deleted"       # removed by retention (tombstone kept)


# --------------------------------------------------------------------------
# Export / evidence
# --------------------------------------------------------------------------
class ExportState(StrEnum):
    QUEUED = "queued"
    PREFLIGHT = "preflight"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ExportFormat(StrEnum):
    MP4 = "mp4"
    ORIGINAL = "original"   # stream copy into container, no transcode
    JPEG = "jpeg"


# --------------------------------------------------------------------------
# Events and alarms
# --------------------------------------------------------------------------
class EventType(StrEnum):
    CAMERA_ONLINE = "camera.online"
    CAMERA_OFFLINE = "camera.offline"
    CAMERA_UNAUTHORIZED = "camera.unauthorized"
    VIDEO_LOSS = "video.loss"
    TAMPER = "camera.tamper"
    MOTION = "camera.motion"
    ONVIF_EVENT = "onvif.event"
    ANALYTICS_EVENT = "analytics.event"
    RECORDING_STARTED = "recording.started"
    RECORDING_STOPPED = "recording.stopped"
    RECORDING_FAILED = "recording.failed"
    RECORDING_GAP = "recording.gap"
    STORAGE_WARNING = "storage.warning"
    STORAGE_CRITICAL = "storage.critical"
    STORAGE_FAILED = "storage.failed"
    SERVICE_DEGRADED = "service.degraded"
    SERVICE_FAILED = "service.failed"
    SYSTEM_HEALTH = "system.health"
    USER_LOGIN_FAILED = "user.login.failed"
    USER_LOCKED = "user.locked"
    LICENSE_LIMIT = "license.limit"


class EventSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlarmState(StrEnum):
    NEW = "new"
    ACTIVE = "active"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    CLOSED = "closed"


# --------------------------------------------------------------------------
# Analytics
# --------------------------------------------------------------------------
class AnalyticsClass(StrEnum):
    PERSON = "person"
    VEHICLE = "vehicle"
    FACE = "face"
    LICENSE_PLATE = "license_plate"
    ANIMAL = "animal"
    BAG = "bag"
    UNKNOWN = "unknown"


class AnalyticsSource(StrEnum):
    """CRITICAL: this distinction must never be blurred in UI or reports.

    CAMERA_EDGE      - the camera produced the metadata (ONVIF / vendor).
    SERVER_INFERENCE - Intelligent VMS ran a model on decoded frames.
    EXTERNAL         - a third-party engine pushed results into the API.
    """

    CAMERA_EDGE = "camera_edge"
    SERVER_INFERENCE = "server_inference"
    EXTERNAL = "external"


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------
class StorageState(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    READONLY = "readonly"
    FULL = "full"
    OFFLINE = "offline"


class VolumeKind(StrEnum):
    LOCAL = "local"
    SMB = "smb"
    NFS = "nfs"
    ISCSI = "iscsi"


# --------------------------------------------------------------------------
# PTZ
# --------------------------------------------------------------------------
class PtzMoveKind(StrEnum):
    CONTINUOUS = "continuous"
    RELATIVE = "relative"
    ABSOLUTE = "absolute"
    STOP = "stop"
    PRESET = "preset"


# --------------------------------------------------------------------------
# Service / health
# --------------------------------------------------------------------------
class ServiceState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Commercial
# --------------------------------------------------------------------------
class Edition(StrEnum):
    LITE = "lite"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class FeatureFlag(StrEnum):
    MULTI_SITE = "multi_site"
    FEDERATION = "federation"
    SERVER_ANALYTICS = "server_analytics"
    FACE_RECOGNITION = "face_recognition"
    ANPR = "anpr"
    LDAP = "ldap"
    OIDC = "oidc"
    REDUNDANCY = "redundancy"
    API_ACCESS = "api_access"


class ImplementationStatus(StrEnum):
    """Used by the feature-matrix generator. See GOLDEN RULE 5.2."""

    IMPLEMENTED = "implemented"
    PARTIAL = "partial"
    DESIGNED = "designed"
    NOT_IMPLEMENTED = "not_implemented"
    BLOCKED = "blocked"
