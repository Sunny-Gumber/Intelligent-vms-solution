import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Boolean, Text, UniqueConstraint, JSON, ForeignKey, DateTime, Float, CheckConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class CameraGroupEntity(TimestampMixin, Base):
    """Persist one tenant/site-scoped logical camera group.

    Construction accepts group scope/name metadata; instances represent database
    rows used to organize managed cameras without changing camera identity.
    """

    __tablename__ = "camera_groups"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "site_id",
            "name",
            name="uq_camera_group_tenant_site_name",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class CameraEntity(TimestampMixin, Base):
    """Persist one managed camera and its transport/control-plane configuration.

    Construction accepts mapped camera fields; instances represent database rows.
    SQLAlchemy or database exceptions may surface when invalid data is persisted.
    """

    __tablename__ = "cameras"
    __table_args__ = (UniqueConstraint("stream_key", name="uq_camera_stream_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), default="default")
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    location_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("camera_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    host: Mapped[str] = mapped_column(String(255))
    rtsp_port: Mapped[int] = mapped_column(Integer, default=554)
    main_path: Mapped[str] = mapped_column(Text)
    sub_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    third_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    third_stream_key: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        unique=True,
        index=True,
    )
    username_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_key: Mapped[str] = mapped_column(String(180), index=True)
    media_node_id: Mapped[str] = mapped_column(String(128), default="media-local-01")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    desired_state: Mapped[str] = mapped_column(String(32), default="provisioned")


class CameraCapabilityEntity(TimestampMixin, Base):
    """Persist the latest ONVIF capability snapshot for one camera.

    Construction accepts mapped capability fields; instances represent database
    rows. SQLAlchemy/database exceptions may occur during persistence.
    """

    __tablename__ = "camera_capabilities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), unique=True, index=True
    )
    onvif_xaddr: Mapped[str] = mapped_column(Text)
    device_info_json: Mapped[dict] = mapped_column(JSON, default=dict)
    services_json: Mapped[list] = mapped_column(JSON, default=list)
    features_json: Mapped[dict] = mapped_column(JSON, default=dict)
    profiles_json: Mapped[list] = mapped_column(JSON, default=list)
    main_profile_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sub_profile_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    third_profile_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    probed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    probe_version: Mapped[str] = mapped_column(String(32), default="phase2a-v1")


class ServiceStateEntity(TimestampMixin, Base):
    """Persist small service coordination state keyed by a stable string.

    Construction accepts the mapped key/value fields and returns an ORM row
    instance. Database exceptions may occur when state is persisted.
    """

    __tablename__ = "service_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)


class RecordingPolicyEntity(TimestampMixin, Base):
    """Persist recording policy and recorder sizing values for one camera.

    Construction accepts mapped policy fields; instances represent database rows.
    Uniqueness or other database constraints may raise during persistence.
    """

    __tablename__ = "recording_policies"
    __table_args__ = (
        UniqueConstraint("camera_id", name="uq_recording_policy_camera_id"),
        UniqueConstraint("record_stream_key", name="uq_recording_policy_stream_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(32), default="disabled")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    record_stream_key: Mapped[str] = mapped_column(String(200), index=True)
    recording_node_id: Mapped[str] = mapped_column(String(128), default="media-local-01")
    retention_days: Mapped[int] = mapped_column(Integer, default=7)
    part_duration_ms: Mapped[int] = mapped_column(Integer, default=1000)
    segment_duration_seconds: Mapped[int] = mapped_column(Integer, default=900)
    max_part_size_mb: Mapped[int] = mapped_column(Integer, default=50)


class ManualRecordingSessionEntity(TimestampMixin, Base):
    """Persist one durable operator manual-recording interval intent."""

    __tablename__ = "manual_recording_sessions"
    __table_args__ = (
        CheckConstraint("state IN ('ACTIVE','STOPPED')", name="ck_manual_recording_state"),
        CheckConstraint("max_stop_at >= started_at", name="ck_manual_recording_max_after_start"),
        CheckConstraint(
            "stopped_at IS NULL OR (stopped_at >= started_at AND stopped_at <= max_stop_at)",
            name="ck_manual_recording_stop_bounds",
        ),
        Index(
            "uq_manual_recording_active_owner_camera",
            "tenant_id", "site_id", "camera_id", "operator_subject",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
            sqlite_where=text("state = 'ACTIVE'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    camera_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operator_subject: Mapped[str] = mapped_column(String(256), index=True)
    state: Mapped[str] = mapped_column(String(16), default="ACTIVE", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    max_stop_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RecordingHealthStateEntity(Base):
    """Persist durable completion health for one continuously recorded camera.

    Construction accepts the latest accepted segment/fencing evidence and the
    derived gap deadline. Database exceptions may occur when the state is written.
    """

    __tablename__ = "recording_health_state"

    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True
    )
    last_segment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_segment_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    gap_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    recording_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    assignment_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class CameraHealthStateEntity(Base):
    """Persist the latest health state and hysteresis counters for one camera.

    Construction accepts mapped health fields and returns an ORM row instance.
    Database exceptions may occur when the state is written.
    """

    __tablename__ = "camera_health_state"

    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    path_present: Mapped[bool] = mapped_column(Boolean, default=False)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[dict] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AlarmRuleEntity(TimestampMixin, Base):
    """Persist one tenant-scoped alarm matching rule.

    Construction accepts mapped rule fields; instances represent database rows.
    Database exceptions may occur when invalid or conflicting rows are persisted.
    """

    __tablename__ = "alarm_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    site_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    event_types_json: Mapped[list] = mapped_column(JSON, default=list)
    severities_json: Mapped[list] = mapped_column(JSON, default=list)
    camera_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    alarm_severity: Mapped[str] = mapped_column(String(16), default="high")
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)


class AlarmInstanceEntity(Base):
    """Persist one deduplicated alarm lifecycle instance raised from an event.

    Construction accepts mapped alarm fields; instances represent database rows.
    Uniqueness or other database constraints may raise during persistence.
    """

    __tablename__ = "alarm_instances"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_alarm_instances_dedupe_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    rule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alarm_rules.id", ondelete="RESTRICT"), index=True
    )
    event_id: Mapped[str] = mapped_column(String(128), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    camera_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(24), default="open", index=True)
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(256), nullable=True)


class AIModelEntity(TimestampMixin, Base):
    """Persist metadata for one tenant-visible AI model version.

    Construction accepts mapped model metadata; instances represent database rows.
    Version uniqueness and other database constraints may raise on persistence.
    """

    __tablename__ = "ai_models"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "version", name="uq_ai_model_tenant_name_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    version: Mapped[str] = mapped_column(String(128))
    provider_type: Mapped[str] = mapped_column(String(32))
    artifact_ref: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    labels_json: Mapped[list] = mapped_column(JSON, default=list)
    input_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class CameraAIPolicyEntity(TimestampMixin, Base):
    """Persist the active AI policy and provider configuration for one camera.

    Construction accepts mapped policy fields; instances represent database rows.
    Foreign-key, uniqueness, or other database constraints may raise on persistence.
    """

    __tablename__ = "camera_ai_policies"
    __table_args__ = (
        UniqueConstraint("camera_id", name="uq_camera_ai_policy_camera_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_mode: Mapped[str] = mapped_column(String(32), default="inference")
    model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ai_models.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    stream_role: Mapped[str] = mapped_column(String(16), default="sub")
    sample_fps: Mapped[float] = mapped_column(Float, default=1.0)
    min_confidence: Mapped[float] = mapped_column(Float, default=0.5)
    analytics_json: Mapped[list] = mapped_column(JSON, default=list)
    zones_json: Mapped[list] = mapped_column(JSON, default=list)
    provider_config_json: Mapped[dict] = mapped_column(JSON, default=dict)



class EventOutboxEntity(TimestampMixin, Base):
    """Persist one reliable outbox message and its delivery/claim state.

    Construction accepts mapped message fields; instances represent database rows.
    Database exceptions may occur when claims or delivery state are persisted.
    """

    __tablename__ = "event_outbox"

    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), index=True)
    key_text: Mapped[str] = mapped_column(String(512))
    payload_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    claim_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
