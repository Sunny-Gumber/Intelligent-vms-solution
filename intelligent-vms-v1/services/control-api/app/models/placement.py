import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class InfrastructureNodeEntity(TimestampMixin, Base):
    """Persist one regional infrastructure node and its advertised capacity/load.

    Construction accepts mapped node fields; instances represent database rows.
    Database exceptions may occur when node state is persisted.
    """

    __tablename__ = "infrastructure_nodes"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    region_id: Mapped[str] = mapped_column(String(128), index=True)
    roles_json: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(24), default="active", index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    endpoints_json: Mapped[dict] = mapped_column(JSON, default=dict)
    capacity_json: Mapped[dict] = mapped_column(JSON, default=dict)
    load_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Null means this row has not reported explicit role readiness. Placement
    # then treats a zero media or recording count as unknown, not as spare
    # capacity. The column is additive; nothing is dropped to add it.
    role_readiness_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    authority_mode: Mapped[str] = mapped_column(String(32), default="central_online")
    generation: Mapped[int] = mapped_column(Integer, default=1)


class SiteRegionEntity(TimestampMixin, Base):
    """Persist the region assignment for one tenant/site pair.

    Construction accepts mapped tenant, site, and region fields and returns an ORM
    row instance. The tenant/site uniqueness constraint may raise on persistence.
    """

    __tablename__ = "site_regions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "site_id", name="uq_site_regions_tenant_site"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    site_id: Mapped[str] = mapped_column(String(128), index=True)
    region_id: Mapped[str] = mapped_column(String(128), index=True)


class PlacementAssignmentEntity(TimestampMixin, Base):
    """Persist fenced ownership of one camera role by an infrastructure node.

    Construction accepts mapped placement, lease, generation, and cleanup fields.
    Foreign-key or uniqueness constraints may raise when the row is persisted.
    """

    __tablename__ = "placement_assignments"
    __table_args__ = (
        UniqueConstraint("camera_id", "role", name="uq_placement_camera_role"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    camera_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("cameras.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(24), index=True)
    region_id: Mapped[str] = mapped_column(String(128), index=True)
    node_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("infrastructure_nodes.id", ondelete="RESTRICT"), index=True
    )
    cleanup_node_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    applied_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    reason: Mapped[str] = mapped_column(String(128), default="initial")
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    autonomy_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )



class PlacementRevocationEntity(TimestampMixin, Base):
    """Persist durable revocation evidence for a previous placement generation.

    Construction accepts mapped revocation/authority fields and returns an ORM row.
    Foreign-key or uniqueness constraints may raise during persistence.
    """

    __tablename__ = "placement_revocations"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "node_id",
            "revoked_generation",
            name="uq_placement_revocation_assignment_node_generation",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    assignment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("placement_assignments.id", ondelete="CASCADE"),
        index=True,
    )
    camera_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cameras.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(24), index=True)
    node_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("infrastructure_nodes.id", ondelete="RESTRICT"),
        index=True,
    )
    revoked_generation: Mapped[int] = mapped_column(Integer)
    execution_keys_json: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        default=list,
    )
    reason: Mapped[str] = mapped_column(String(128), default="ownership_changed")
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
