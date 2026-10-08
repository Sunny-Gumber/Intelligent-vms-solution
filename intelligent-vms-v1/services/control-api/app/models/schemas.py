from datetime import datetime
import math
from uuid import UUID
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator


class CameraCreate(BaseModel):
    """Validate input used to create a managed camera.

    Construction returns a validated camera request model and may raise Pydantic
    validation errors for incompatible field values.
    """

    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    site_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    host: str = Field(min_length=1, max_length=255)
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    source_protocol: Literal["rtsp", "rtsps"] = "rtsp"
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    main_path: str = Field(min_length=1, max_length=2048)
    sub_path: str | None = Field(default=None, max_length=2048)
    third_path: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_secure_pin(self):
        """Require explicit RTSPS when approving a device certificate fingerprint."""
        if self.source_fingerprint and self.source_protocol != "rtsps":
            raise ValueError("certificate fingerprint requires RTSPS")
        return self

    @field_validator("main_path", "sub_path", "third_path")
    @classmethod
    def ensure_path_prefix(cls, value: str | None):
        """Normalize non-empty RTSP paths to begin with a slash.

        Args:
            value: Main/sub stream path supplied by the caller.

        Returns:
            None/empty input unchanged, otherwise a slash-prefixed path.
        """
        if value and not value.startswith("/"):
            return "/" + value
        return value


class CameraRead(BaseModel):
    """Serialize camera configuration and live-access metadata.

    Fields represent the stored camera plus generated stream/access values.
    Validation may raise for incompatible input types.
    """

    id: str
    tenant_id: str
    site_id: str
    name: str
    location_description: str | None = None
    group_id: str | None = None
    host: str
    rtsp_port: int
    source_protocol: Literal["rtsp", "rtsps"] = "rtsp"
    source_fingerprint: str | None = None
    stream_key: str
    third_stream_key: str | None = None
    available_live_roles: list[Literal["main", "sub", "third"]] = Field(default_factory=list)
    media_node_id: str
    enabled: bool
    desired_state: str
    created_at: datetime
    webrtc_url: str | None
    hls_url: str | None
    third_webrtc_url: str | None = None
    third_hls_url: str | None = None


class CameraUpdate(BaseModel):
    """Validate partial logical camera metadata updates.

    Name, location and group can be changed without changing tenant/site, camera
    identity or stable stream keys.
    """

    name: str | None = Field(default=None, min_length=1, max_length=256)
    location_description: str | None = Field(default=None, max_length=1024)
    group_id: str | None = Field(default=None, max_length=36)

    @model_validator(mode="after")
    def require_change(self):
        """Require at least one explicitly supplied metadata field.

        Returns:
            Validated update request.

        Raises:
            ValueError: If no metadata field was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one camera metadata field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("camera name cannot be null")
        return self


class CameraCredentialUpdate(BaseModel):
    """Validate camera credential rotation input.

    Explicit null values clear a stored username/password while omitted values
    preserve the existing encrypted value.
    """

    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_credential_change(self):
        """Require at least one explicitly supplied credential field.

        Returns:
            Validated credential update request.

        Raises:
            ValueError: If neither credential field was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one credential field is required")
        return self


class CameraReplacement(BaseModel):
    """Validate replacement of the physical source behind a logical camera.

    Replacement preserves camera ID and stable stream keys while changing the
    validated source transport fields and optional credentials.
    """

    host: str = Field(min_length=1, max_length=255)
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    source_protocol: Literal["rtsp", "rtsps"] = "rtsp"
    source_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    main_path: str = Field(min_length=1, max_length=2048)
    sub_path: str | None = Field(default=None, max_length=2048)
    third_path: str | None = Field(default=None, max_length=2048)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def require_secure_replacement_pin(self):
        """Require explicit RTSPS when approving the replacement certificate."""
        if self.source_fingerprint and self.source_protocol != "rtsps":
            raise ValueError("certificate fingerprint requires RTSPS")
        return self

    @field_validator("main_path", "sub_path", "third_path")
    @classmethod
    def ensure_replacement_path_prefix(cls, value: str | None):
        """Normalize replacement RTSP paths to begin with a slash.

        Args:
            value: Main/sub stream path supplied for the replacement source.

        Returns:
            None/empty input unchanged, otherwise a slash-prefixed path.
        """
        if value and not value.startswith("/"):
            return "/" + value
        return value


class CameraGroupCreate(BaseModel):
    """Validate creation of a tenant/site-scoped camera group."""

    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    site_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1024)


class CameraGroupUpdate(BaseModel):
    """Validate partial camera-group metadata changes."""

    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def require_group_change(self):
        """Require at least one explicitly supplied group field.

        Returns:
            Validated group update request.

        Raises:
            ValueError: If no field was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one group field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("camera group name cannot be null")
        return self


class CameraGroupRead(BaseModel):
    """Serialize a tenant/site-scoped logical camera group."""

    id: str
    tenant_id: str
    site_id: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class CameraHealth(BaseModel):
    """Serialize immediate media-path health for one camera.

    Fields expose camera/stream identity, path readiness, tracks and bounded
    diagnostic detail. Validation may raise for incompatible input types.
    """

    camera_id: str
    stream_key: str
    path_present: bool
    ready: bool = False
    tracks: list[str] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class EventIn(BaseModel):
    """Validate a normalized event accepted by the VMS event API.

    Construction enforces bounded identifiers, confidence, severity and attributes
    and may raise Pydantic validation errors.
    """

    event_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    site_id: str = Field(min_length=1, max_length=128)
    camera_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    event_type: str = Field(min_length=1, max_length=128)
    object_type: str | None = Field(default=None, max_length=128)
    source: Literal["camera", "vms", "ai", "integration"]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    zone_id: str | None = Field(default=None, max_length=128)
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    snapshot_uri: str | None = Field(default=None, max_length=2048)
    recording_start: datetime | None = None
    recording_end: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def validate_event_attributes(cls, value):
        """Bound event attributes and normalize attribute keys.

        Args:
            value: Event attribute mapping.

        Returns:
            Mapping with string keys truncated to 128 characters.

        Raises:
            ValueError: If key count or serialized representation exceeds bounds.
        """
        if len(value) > 128 or len(repr(value)) > 65536:
            raise ValueError("event attributes exceed bounded size")
        return {str(k)[:128]: v for k, v in value.items()}


class EventRead(EventIn):
    """Serialize a normalized event with optional ingest timestamp.

    Inherits EventIn validation and adds the time recorded by the event store.
    """

    ingested_at: datetime | None = None



class EventCenterRead(BaseModel):
    """Serialize the credential/URL-safe Event Center projection."""

    event_id: str
    tenant_id: str
    site_id: str
    camera_id: str
    timestamp: datetime
    event_type: str
    object_type: str | None = None
    source: Literal["camera", "vms", "ai", "integration"]
    confidence: float | None = None
    zone_id: str | None = None
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    recording_start: datetime | None = None
    recording_end: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    ingested_at: datetime | None = None


class EventHistoryPage(BaseModel):
    """Serialize one bounded deterministic page of authorized Event Center history."""

    items: list[EventCenterRead] = Field(default_factory=list)
    next_before: datetime | None = None
    next_before_id: str | None = None

class CameraHealthStateRead(BaseModel):
    """Serialize persisted camera health and transition state.

    Fields expose tenant/site identity, state, readiness, observation/change times
    and diagnostic detail.
    """

    camera_id: str
    tenant_id: str
    site_id: str
    name: str
    state: Literal["unknown", "degraded", "online", "offline"]
    path_present: bool
    ready: bool
    observed_at: datetime
    changed_at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class HealthSummary(BaseModel):
    """Serialize aggregate camera-health and monitor statistics.

    Fields contain state counts plus the latest monitor execution counters.
    Validation may raise for incompatible input types.
    """

    total: int
    unknown: int = 0
    degraded: int = 0
    online: int = 0
    offline: int = 0
    monitor_last_duration_seconds: float = 0.0
    monitor_last_scanned: int = 0
    monitor_last_changed: int = 0
    monitor_media_errors: int = 0


class OnvifDiscoverRequest(BaseModel):
    """Validate tenant/site-scoped WS-Discovery input.

    Construction binds discovery to an authorized tenant/site and constrains the
    socket timeout to 0.5 through 5 seconds.
    """

    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    site_id: str | None = Field(default=None, min_length=1, max_length=128)
    timeout_seconds: float = Field(default=2.5, ge=0.5, le=5.0)


class OnvifDiscoveredDevice(BaseModel):
    """Serialize one vendor-neutral WS-Discovery result.

    Fields expose endpoint reference, advertised addresses, scopes and types.
    """

    endpoint_reference: str | None = None
    xaddrs: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    types: list[str] = Field(default_factory=list)


class OnvifConnection(BaseModel):
    """Validate connection parameters for one ONVIF device.

    Construction validates bounded connection fields and returns a connection
    model or raises Pydantic validation errors.
    """

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    scheme: Literal["http", "https"] = "http"
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=512)
    device_service_path: str = Field(default="/onvif/device_service", min_length=1, max_length=1024)


class OnvifProbeRequest(OnvifConnection):
    """Validate tenant/site-scoped ONVIF probe input."""

    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    site_id: str | None = Field(default=None, min_length=1, max_length=128)


class OnvifSerialOnboardRequest(BaseModel):
    """Validate site-local ONVIF onboarding by exact device serial number."""

    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    site_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    serial_number: str = Field(min_length=1, max_length=256)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=512)
    timeout_seconds: float = Field(default=2.5, ge=0.5, le=5.0)
    main_profile_token: str | None = Field(default=None, max_length=255)
    sub_profile_token: str | None = Field(default=None, max_length=255)
    third_profile_token: str | None = Field(default=None, max_length=255)


class OnvifQrOnboardRequest(BaseModel):
    """Validate a decoded credential-safe VMS QR onboarding payload."""

    qr_payload: str = Field(min_length=1, max_length=4096)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=512)



class OnvifProfile(BaseModel):
    """Serialize normalized ONVIF media-profile metadata.

    Fields expose token, codec/resolution/rate information and sanitized stream URI
    when available.
    """

    token: str
    name: str | None = None
    encoding: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate_kbps: int | None = None
    video_encoder_configuration_token: str | None = None
    video_source_configuration_token: str | None = None
    video_source_token: str | None = None
    stream_uri: str | None = None


class OnvifProbeRead(BaseModel):
    """Serialize a complete ONVIF probe result.

    Fields combine validated device-service address, capabilities, profiles and
    recommended main/sub profile tokens.
    """

    xaddr: str
    device_info: dict[str, Any] = Field(default_factory=dict)
    services: list[dict[str, Any]] = Field(default_factory=list)
    features: dict[str, Any] = Field(default_factory=dict)
    profiles: list[OnvifProfile] = Field(default_factory=list)
    recommended_main_profile_token: str | None = None
    recommended_sub_profile_token: str | None = None


class OnvifOnboardRequest(OnvifProbeRequest):
    """Validate ONVIF onboarding input for a managed camera.

    Extends OnvifConnection with required tenant/site/name and optional profile choices.
    Validation may raise for incompatible connection or field values.
    """

    site_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    main_profile_token: str | None = None
    sub_profile_token: str | None = None
    third_profile_token: str | None = None
    expected_serial_number: str | None = Field(default=None, min_length=1, max_length=256)


class CameraCapabilityRead(BaseModel):
    """Serialize the stored ONVIF capability snapshot for a camera.

    Fields expose device/service/profile metadata and selected profile tokens with
    the probe timestamp.
    """

    camera_id: str
    onvif_xaddr: str
    device_info: dict[str, Any]
    services: list[dict[str, Any]]
    features: dict[str, Any]
    profiles: list[dict[str, Any]]
    main_profile_token: str | None
    sub_profile_token: str | None
    third_profile_token: str | None = None
    probed_at: datetime


class PtzCapabilitiesRead(BaseModel):
    """Serialize server-authoritative ONVIF PTZ capabilities for one camera."""

    camera_id: str
    ptz: bool
    pan_tilt: bool
    zoom: bool
    presets: bool = False
    software_supported: bool = True
    hardware_verified: bool = False


class PtzMoveRequest(BaseModel):
    """Validate a normalized continuous PTZ vector plus monotonic client generation."""

    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    tilt: float = Field(default=0.0, ge=-1.0, le=1.0)
    zoom: float = Field(default=0.0, ge=-1.0, le=1.0)
    generation: int = Field(ge=1, le=2147483647)
    context_id: UUID

    @field_validator("pan", "tilt", "zoom")
    @classmethod
    def finite_ptz_vector(cls, value: float):
        """Reject NaN/Infinity before values can reach ONVIF SOAP."""
        if not math.isfinite(value):
            raise ValueError("PTZ vector values must be finite")
        return value

    @model_validator(mode="after")
    def nonzero_ptz_vector(self):
        """Require movement in at least one PTZ axis."""
        if self.pan == 0.0 and self.tilt == 0.0 and self.zoom == 0.0:
            raise ValueError("PTZ move vector must not be zero")
        return self


class PtzStopRequest(BaseModel):
    """Carry a PTZ context plus monotonic generation for stop-priority fencing."""

    generation: int = Field(ge=1, le=2147483647)
    context_id: UUID


class PtzCommandRead(BaseModel):
    """Serialize a safe PTZ command acknowledgement."""

    camera_id: str
    state: Literal["moving", "stopped"]
    generation: int


class OnvifManagedProfileUpdate(BaseModel):
    """Validate reassignment of a managed main/sub/third ONVIF profile role."""

    profile_token: str = Field(min_length=1, max_length=255)


class OnvifCodecProfileSelect(BaseModel):
    """Validate codec-driven selection of an existing ONVIF media profile."""

    encoding: Literal["H264", "H265", "MJPEG"]
    preferred_profile_token: str | None = Field(default=None, max_length=255)



class OnvifEncoderUpdate(BaseModel):
    """Validate partial ONVIF video-encoder changes.

    Fields map to standard Media/Media2 encoder configuration values. Unsupported
    fields or values are rejected against device-advertised options before write.
    """

    encoding: Literal["H264", "H265", "JPEG", "MPEG4"] | None = None
    width: int | None = Field(default=None, ge=16, le=32768)
    height: int | None = Field(default=None, ge=16, le=32768)
    fps: float | None = Field(default=None, gt=0, le=240)
    bitrate_kbps: int | None = Field(default=None, ge=1, le=1000000)
    encoding_interval: int | None = Field(default=None, ge=1, le=1000)
    gov_length: int | None = Field(default=None, ge=1, le=10000)
    quality: float | None = Field(default=None, ge=0, le=100)
    bitrate_mode: Literal["CBR", "VBR"] | None = None

    @model_validator(mode="after")
    def require_encoder_change(self):
        """Require at least one encoder field.

        Returns:
            Validated encoder update.

        Raises:
            ValueError: If no encoder setting was supplied or only one resolution
                dimension is provided.
        """
        if not self.model_fields_set:
            raise ValueError("at least one encoder field is required")
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be supplied together")
        return self


class OnvifEncoderRead(BaseModel):
    """Serialize current encoder state plus device-advertised options."""

    role: Literal["main", "sub", "third"]
    profile_token: str
    configuration_token: str
    current: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class OnvifImagingUpdate(BaseModel):
    """Validate partial standard ONVIF imaging changes."""

    brightness: float | None = Field(default=None, ge=-10000, le=10000)
    contrast: float | None = Field(default=None, ge=-10000, le=10000)
    saturation: float | None = Field(default=None, ge=-10000, le=10000)
    sharpness: float | None = Field(default=None, ge=-10000, le=10000)
    hue: float | None = Field(default=None, ge=-10000, le=10000)
    ir_cut_filter: Literal["ON", "OFF", "AUTO"] | None = None
    white_balance_mode: Literal["AUTO", "MANUAL"] | None = None
    white_balance_r_gain: float | None = Field(default=None, ge=-10000, le=10000)
    white_balance_b_gain: float | None = Field(default=None, ge=-10000, le=10000)
    backlight_mode: Literal["ON", "OFF"] | None = None
    backlight_level: float | None = Field(default=None, ge=-10000, le=10000)
    wdr_mode: Literal["ON", "OFF"] | None = None
    wdr_level: float | None = Field(default=None, ge=-10000, le=10000)
    exposure_mode: Literal["AUTO", "MANUAL"] | None = None
    exposure_priority: Literal["LowNoise", "FrameRate"] | None = None
    exposure_time: float | None = Field(default=None, ge=0, le=10000000)
    gain: float | None = Field(default=None, ge=-10000, le=10000)
    iris: float | None = Field(default=None, ge=-10000, le=10000)
    anti_flicker: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def require_imaging_change(self):
        """Require at least one imaging field.

        Returns:
            Validated imaging update.

        Raises:
            ValueError: If no imaging setting was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one imaging field is required")
        return self


class OnvifImagingRead(BaseModel):
    """Serialize current ONVIF imaging state and supported options."""

    video_source_token: str
    current: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class OnvifRotationUpdate(BaseModel):
    """Validate ONVIF video-source rotation/mirror changes."""

    rotation_mode: Literal["OFF", "ON", "AUTO"] | None = None
    rotation_degree: int | None = Field(default=None, ge=0, le=359)
    mirror: bool | None = None
    flip: bool | None = None

    @model_validator(mode="after")
    def require_rotation_change(self):
        """Require at least one orientation field.

        Returns:
            Validated orientation update.

        Raises:
            ValueError: If no orientation field was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one orientation field is required")
        if self.flip is not None and any(
            field in self.model_fields_set
            for field in {"rotation_mode", "rotation_degree", "mirror"}
        ):
            raise ValueError("flip cannot be combined with rotation or mirror fields")
        return self


class OnvifDateTimeUpdate(BaseModel):
    """Validate standard ONVIF camera date/time configuration."""

    mode: Literal["Manual", "NTP"]
    daylight_savings: bool = False
    timezone: str | None = Field(default=None, max_length=128)
    utc_datetime: datetime | None = None

    @model_validator(mode="after")
    def validate_manual_time(self):
        """Require UTC date/time for Manual mode.

        Returns:
            Validated date/time request.

        Raises:
            ValueError: If Manual mode omits UTC date/time.
        """
        if self.mode == "Manual" and self.utc_datetime is None:
            raise ValueError("utc_datetime is required for Manual mode")
        if self.utc_datetime is not None and self.utc_datetime.tzinfo is None:
            raise ValueError("utc_datetime must include timezone information")
        return self


class OnvifDateTimeRead(BaseModel):
    """Serialize bounded ONVIF camera date/time state."""

    mode: str | None = None
    daylight_savings: bool | None = None
    timezone: str | None = None
    utc_datetime: datetime | None = None


class OnvifIrUpdate(BaseModel):
    """Validate standard ONVIF auxiliary IR-lamp control."""

    mode: Literal["On", "Off", "Auto"]


class OnvifVideoSourceModeUpdate(BaseModel):
    """Validate selection of an advertised ONVIF video-source mode."""

    mode_token: str = Field(min_length=1, max_length=255)


class OnvifVideoStandardUpdate(BaseModel):
    """Validate PAL/NTSC selection through an explicitly labelled source mode."""

    standard: Literal["PAL", "NTSC"]


class OnvifOsdCreate(BaseModel):
    """Validate creation of a text OSD bound to a managed profile."""

    text: str = Field(min_length=1, max_length=256)
    position_type: Literal["Custom", "UpperLeft", "UpperRight", "LowerLeft", "LowerRight"] = "Custom"
    x: float = Field(default=0.0, ge=-1.0, le=1.0)
    y: float = Field(default=0.0, ge=-1.0, le=1.0)
    osd_type: Literal["Plain", "Date", "Time", "DateAndTime"] = "Plain"


class OnvifOsdUpdate(BaseModel):
    """Validate partial text OSD modification."""

    text: str | None = Field(default=None, min_length=1, max_length=256)
    x: float | None = Field(default=None, ge=-1.0, le=1.0)
    y: float | None = Field(default=None, ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def require_osd_change(self):
        """Require at least one OSD field.

        Returns:
            Validated OSD patch.

        Raises:
            ValueError: If no OSD field was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one OSD field is required")
        return self


class OnvifPrivacyMaskCreate(BaseModel):
    """Validate a normalized Media2 privacy-mask polygon."""

    points: list[tuple[float, float]] = Field(min_length=3, max_length=32)
    enabled: bool = True
    mask_type: Literal["Color", "Blurred", "Pixelized"] = "Color"

    @field_validator("points")
    @classmethod
    def validate_mask_points(cls, value):
        """Require normalized Media2 mask coordinates.

        Args:
            value: Polygon coordinates.

        Returns:
            Unchanged normalized polygon.

        Raises:
            ValueError: If any coordinate is outside the Media2 -1..1 range.
        """
        if any(x < -1 or x > 1 or y < -1 or y > 1 for x, y in value):
            raise ValueError("privacy-mask coordinates must be between -1 and 1")
        return value


class OnvifPrivacyMaskUpdate(BaseModel):
    """Validate partial Media2 privacy-mask modification."""

    points: list[tuple[float, float]] | None = Field(
        default=None,
        min_length=3,
        max_length=32,
    )
    enabled: bool | None = None
    mask_type: Literal["Color", "Blurred", "Pixelized"] | None = None

    @field_validator("points")
    @classmethod
    def validate_updated_mask_points(cls, value):
        """Require normalized Media2 mask coordinates when supplied.

        Args:
            value: Optional polygon coordinates.

        Returns:
            Unchanged polygon or None.

        Raises:
            ValueError: If any coordinate is outside -1..1.
        """
        if value is not None and any(
            x < -1 or x > 1 or y < -1 or y > 1 for x, y in value
        ):
            raise ValueError("privacy-mask coordinates must be between -1 and 1")
        return value

    @model_validator(mode="after")
    def require_mask_change(self):
        """Require at least one privacy-mask field.

        Returns:
            Validated mask patch.

        Raises:
            ValueError: If no mask field was supplied.
        """
        if not self.model_fields_set:
            raise ValueError("at least one privacy-mask field is required")
        return self


class OnvifCameraNameOsdUpdate(BaseModel):
    """Validate camera-name overlay create/update request."""

    osd_token: str | None = Field(default=None, min_length=1, max_length=255)
    position_type: Literal[
        "Custom",
        "UpperLeft",
        "UpperRight",
        "LowerLeft",
        "LowerRight",
    ] = "UpperLeft"
    x: float | None = Field(default=None, ge=-1.0, le=1.0)
    y: float | None = Field(default=None, ge=-1.0, le=1.0)


class RecordingPolicyUpdate(BaseModel):
    """Validate recording-policy changes for one camera.

    Construction enforces recording mode, retention and recorder segment/part
    bounds and may raise Pydantic validation errors.
    """

    enabled: bool = True
    mode: Literal["disabled", "continuous", "event", "scheduled"] = "continuous"
    retention_days: int = Field(default=7, ge=1, le=3650)
    part_duration_ms: int = Field(default=1000, ge=250, le=10000)
    segment_duration_seconds: int = Field(default=900, ge=30, le=21600)
    max_part_size_mb: int = Field(default=50, ge=5, le=1024)


class RecordingPolicyRead(BaseModel):
    """Serialize the persisted recording policy for one camera.

    Fields expose mode, recorder ownership, retention and segmentation settings plus
    update time.
    """

    camera_id: str
    mode: str
    enabled: bool
    record_stream_key: str
    recording_node_id: str
    retention_days: int
    part_duration_ms: int
    segment_duration_seconds: int
    max_part_size_mb: int
    updated_at: datetime


class ManualRecordingRead(BaseModel):
    """Serialize safe durable manual-recording session metadata."""

    id: str
    tenant_id: str
    site_id: str
    camera_id: str | None
    operator_subject: str
    state: Literal["ACTIVE", "STOPPED"]
    started_at: datetime
    stopped_at: datetime | None
    effective_max_stop_at: datetime
    duration: float | None
    download_ready: bool


class RecordingTimespan(BaseModel):
    """Serialize one playable recording interval.

    Fields expose start, duration and end values. Validation may raise for
    incompatible input types.
    """

    start: datetime
    duration: float
    end: datetime


class AlarmRuleCreate(BaseModel):
    """Validate input used to create an alarm matching rule.

    Construction enforces bounded event/camera lists, severity values and cooldown
    range and may raise Pydantic validation errors.
    """

    tenant_id: str = "default"
    site_id: str | None = None
    name: str = Field(min_length=1, max_length=256)
    enabled: bool = True
    event_types: list[str] = Field(min_length=1, max_length=64)
    severities: list[Literal["info", "low", "medium", "high", "critical"]] = Field(default_factory=list)
    camera_ids: list[str] = Field(default_factory=list, max_length=1000)
    alarm_severity: Literal["info", "low", "medium", "high", "critical"] = "high"
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class AlarmRuleRead(BaseModel):
    """Serialize a persisted alarm rule and timestamps.

    Fields expose matching scope, event/severity filters, camera set and alarm
    lifecycle configuration.
    """

    id: str
    tenant_id: str
    site_id: str | None
    name: str
    enabled: bool
    event_types: list[str]
    severities: list[str]
    camera_ids: list[str]
    alarm_severity: str
    cooldown_seconds: int
    created_at: datetime
    updated_at: datetime


class AlarmRuleUpdate(BaseModel):
    """Validate partial updates to an existing alarm rule.

    Omitted fields retain persisted values. Explicit nulls are rejected because
    these mutable fields are non-nullable; empty camera lists mean wildcard.
    Supplied values retain the create-model bounds and may raise validation errors.
    """

    name: str | None = Field(default=None, min_length=1, max_length=256)
    enabled: bool | None = None
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=64)
    severities: list[Literal["info", "low", "medium", "high", "critical"]] | None = None
    camera_ids: list[str] | None = Field(default=None, max_length=1000)
    alarm_severity: Literal["info", "low", "medium", "high", "critical"] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86400)

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> "AlarmRuleUpdate":
        """Reject null PATCH values before persistence while allowing omission.

        Returns:
            Validated partial rule update.

        Raises:
            ValueError: If any explicitly supplied mutable field is null.
        """
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Alarm-rule update fields cannot be null")
        return self


class AlarmInstanceRead(BaseModel):
    """Serialize one alarm instance and acknowledgement state.

    Fields expose source event/rule identity, scope, state, message and lifecycle
    timestamps.
    """

    id: str
    rule_id: str
    event_id: str
    tenant_id: str
    site_id: str
    camera_id: str
    event_type: str
    severity: str
    state: Literal["open", "acknowledged", "closed"]
    message: str
    opened_at: datetime
    last_event_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: str | None


class DiagnosticRead(BaseModel):
    """Serialize one media diagnostic sample for a camera.

    Fields expose byte rates and optional RTP packet/loss/jitter counters captured
    at the sample time.
    """

    camera_id: str
    stream_key: str
    sampled_at: datetime
    path_state: str | None = None
    inbound_bytes: int | None = None
    outbound_bytes: int | None = None
    inbound_mbps: float | None = None
    outbound_mbps: float | None = None
    rtp_packets: int | None = None
    rtp_packets_lost: int | None = None
    rtp_packets_in_error: int | None = None
    rtp_jitter: float | None = None


AI_ANALYTIC_TYPES = Literal["human", "vehicle", "tripwire", "intrusion", "anpr", "face", "object_count"]


class AIModelCreate(BaseModel):
    """Validate registration metadata for one AI model version.

    Construction enforces provider, artifact, label and optional input-size bounds
    and may raise Pydantic validation errors.
    """

    tenant_id: str = "default"
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    provider_type: Literal["onnx", "external"]
    artifact_ref: str = Field(min_length=1, max_length=512)
    sha256: str | None = None
    labels: list[str] = Field(default_factory=list, max_length=512)
    input_width: int | None = Field(default=None, ge=16, le=16384)
    input_height: int | None = Field(default=None, ge=16, le=16384)
    enabled: bool = True


class AIModelRead(BaseModel):
    """Serialize persisted AI model metadata.

    Fields expose identity, version/provider, artifact integrity metadata, labels,
    input dimensions, enabled state and timestamps.
    """

    id: str
    tenant_id: str
    name: str
    version: str
    provider_type: str
    artifact_ref: str
    sha256: str | None
    labels: list[str]
    input_width: int | None
    input_height: int | None
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AIPoint(BaseModel):
    """Validate one normalized two-dimensional AI geometry point.

    Coordinates are constrained to the inclusive 0..1 range; invalid values raise
    Pydantic validation errors.
    """

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class AIZone(BaseModel):
    """Validate a polygon or line zone used by AI analytics.

    Construction validates zone identity, bounded normalized points and
    geometry-specific point counts.
    """

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    kind: Literal["polygon", "line"] = "polygon"
    points: list[AIPoint] = Field(min_length=2, max_length=32)

    @model_validator(mode="after")
    def validate_geometry(self):
        """Enforce point-count rules for line and polygon zones.

        Returns:
            The validated zone instance.

        Raises:
            ValueError: If a line has other than two points or a polygon has
                fewer than three points.
        """
        if self.kind == "line" and len(self.points) != 2:
            raise ValueError("line zones require exactly two points")
        if self.kind == "polygon" and len(self.points) < 3:
            raise ValueError("polygon zones require at least three points")
        return self


class AIPolicyUpdate(BaseModel):
    """Validate AI policy configuration for one camera.

    Construction validates model/stream selection, sampling/confidence bounds,
    analytics, zones and bounded provider configuration.
    """

    enabled: bool = False
    source_mode: Literal["inference"] = "inference"
    model_id: str | None = None
    stream_role: Literal["main", "sub"] = "sub"
    sample_fps: float = Field(default=1.0, ge=0.1, le=30.0)
    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    analytics: list[AI_ANALYTIC_TYPES] = Field(default_factory=list, max_length=16)
    zones: list[AIZone] = Field(default_factory=list, max_length=64)
    provider_config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_inference_model(self):
        """Enforce enabled-policy model/analytic and provider bounds.

        Returns:
            The validated AI policy instance.

        Raises:
            ValueError: If an enabled policy lacks analytics/model selection or
                provider configuration contains too many keys.
        """
        if self.enabled and not self.analytics:
            raise ValueError("enabled AI policy requires at least one analytic")
        if self.enabled and self.source_mode == "inference" and not self.model_id:
            raise ValueError("model_id is required for enabled inference mode")
        if len(self.provider_config) > 64:
            raise ValueError("provider_config has too many keys")
        return self


class AIPolicyRead(BaseModel):
    """Serialize the active AI policy for one camera.

    Fields expose source/model selection, sampling/confidence, analytics, zones,
    provider configuration and update time. ``provider_config`` on this public
    model is readback: the policy serializer removes secret-like keys first.
    """

    camera_id: str
    enabled: bool
    source_mode: str
    model_id: str | None
    stream_role: str
    sample_fps: float
    min_confidence: float
    analytics: list[str]
    zones: list[dict[str, Any]]
    provider_config: dict[str, Any]
    updated_at: datetime


class AIDetection(BaseModel):
    """Validate one normalized AI detection result.

    Construction enforces analytic type, confidence, optional normalized bounding
    box and bounded attributes.
    """

    event_type: AI_ANALYTIC_TYPES
    object_type: str | None = Field(default=None, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    zone_id: str | None = Field(default=None, max_length=128)
    severity: Literal["info", "low", "medium", "high", "critical"] = "info"
    bbox: list[float] | None = Field(default=None, min_length=4, max_length=4)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def validate_attributes(cls, value):
        """Bound AI detection attributes and normalize attribute keys.

        Args:
            value: Detection attribute mapping.

        Returns:
            Mapping with string keys truncated to 128 characters.

        Raises:
            ValueError: If key count or serialized representation exceeds bounds.
        """
        if len(value) > 64 or len(repr(value)) > 8192:
            raise ValueError("AI detection attributes exceed bounded size")
        return {str(k)[:128]: v for k, v in value.items()}

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value):
        """Require optional bounding-box coordinates to be normalized.

        Args:
            value: Four-coordinate bounding box or None.

        Returns:
            The unchanged bounding box when every coordinate is within 0..1.

        Raises:
            ValueError: If any supplied coordinate lies outside 0..1.
        """
        if value is not None and any((v < 0.0 or v > 1.0) for v in value):
            raise ValueError("bbox coordinates must be normalized to 0..1")
        return value


class AIResultIn(BaseModel):
    """Validate a batch of AI detections observed for one camera/model.

    Fields identify the camera/model/result, observation time and a bounded
    detection list.
    """

    camera_id: str
    model_id: str
    result_id: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    detections: list[AIDetection] = Field(max_length=256)


class AIIngestRead(BaseModel):
    """Serialize the number of AI-derived events accepted for ingest.

    Validation may raise for incompatible input types.
    """

    accepted_events: int


class AIStatusRead(BaseModel):
    """Serialize aggregate AI policy counts used by status endpoints.

    Fields report enabled, inference and camera-metadata policy totals.
    """

    enabled_policies: int
    inference_policies: int
    camera_metadata_policies: int
