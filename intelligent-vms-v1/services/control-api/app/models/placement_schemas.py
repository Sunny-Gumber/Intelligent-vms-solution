from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

NodeRole = Literal["media", "recording", "ai"]
NodeState = Literal["active", "draining", "maintenance"]
AuthorityMode = Literal["central_online", "regional_autonomous", "fenced_degraded"]


def _validate_endpoints(value: dict[str, str] | None):
    if value is None:
        return value
    allowed = {
        "api_url",
        "webrtc_public_base",
        "hls_public_base",
        "playback_internal_url",
        "metrics_url",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"unsupported endpoint keys: {sorted(unknown)}")
    for key, raw in value.items():
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{key} must be an http/https URL with host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{key} must not contain credentials/query/fragment")
    return value


class NodeUpsert(BaseModel):
    """Validate node registration/update input.

    Parameters correspond to node identity, region, roles, state, endpoints and
    capacity. Validation returns a normalized model instance and may raise
    Pydantic validation errors for invalid roles, endpoints or capacity values.
    """

    name: str = Field(min_length=1, max_length=256)
    region_id: str = Field(min_length=1, max_length=128)
    roles: list[NodeRole] = Field(min_length=1, max_length=3)
    state: NodeState = "active"
    enabled: bool = True
    endpoints: dict[str, str] = Field(default_factory=dict)
    capacity: dict[str, float] = Field(default_factory=dict)

    @field_validator("roles")
    @classmethod
    def unique_roles(cls, value):
        """Require role values to be unique.

        Args:
            value: Candidate role list.

        Returns:
            The unchanged role list when all values are unique.

        Raises:
            ValueError: If a role appears more than once.
        """
        if len(value) != len(set(value)):
            raise ValueError("roles must be unique")
        return value

    @field_validator("endpoints")
    @classmethod
    def safe_endpoints(cls, value):
        """Validate node endpoint keys and URL safety constraints.

        Args:
            value: Endpoint mapping supplied by the caller.

        Returns:
            The validated endpoint mapping.

        Raises:
            ValueError: If endpoint keys or URL forms violate the allowlist.
        """
        return _validate_endpoints(value)

    @field_validator("capacity")
    @classmethod
    def nonnegative_capacity(cls, value):
        """Require all advertised capacity values to be non-negative.

        Args:
            value: Capacity mapping to validate.

        Returns:
            The unchanged capacity mapping when valid.

        Raises:
            ValueError: If any capacity value is negative.
        """
        if any(float(v) < 0 for v in value.values()):
            raise ValueError("capacity values must be non-negative")
        return value


class NodeHeartbeat(BaseModel):
    """Validate a node heartbeat payload.

    Parameters describe current load, authority mode and observation time.
    Construction returns a normalized model or raises Pydantic validation errors
    for unsupported fields, negative load or timezone-naive timestamps.
    """

    model_config = ConfigDict(extra="forbid")

    load: dict[str, float] = Field(default_factory=dict)
    authority_mode: AuthorityMode = "central_online"
    observed_at: datetime

    @field_validator("load")
    @classmethod
    def nonnegative_load(cls, value):
        """Require all reported node load values to be non-negative.

        Args:
            value: Load mapping to validate.

        Returns:
            The unchanged load mapping when valid.

        Raises:
            ValueError: If any load value is negative.
        """
        if any(float(v) < 0 for v in value.values()):
            raise ValueError("load values must be non-negative")
        return value

    @field_validator("observed_at")
    @classmethod
    def timezone_aware_observed_at(cls, value: datetime):
        """Require heartbeat observation timestamps to include a timezone.

        Args:
            value: Candidate observation timestamp.

        Returns:
            The unchanged timezone-aware timestamp.

        Raises:
            ValueError: If the timestamp is timezone-naive.
        """
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value



class NodeRead(BaseModel):
    """Serialize infrastructure-node state returned by the control API.

    Fields expose identity, placement metadata, capacity/load, heartbeat,
    authority mode and generation. Model validation may raise for incompatible
    input types.
    """

    id: str
    name: str
    region_id: str
    roles: list[str]
    state: str
    enabled: bool
    endpoints: dict[str, Any]
    capacity: dict[str, Any]
    load: dict[str, Any]
    heartbeat_at: datetime
    authority_mode: AuthorityMode
    generation: int


class SiteRegionUpdate(BaseModel):
    """Validate a tenant/site region assignment request.

    Construction returns a validated assignment model and may raise Pydantic
    validation errors when site or region identifiers violate field bounds.
    """

    tenant_id: str = "default"
    site_id: str = Field(min_length=1, max_length=128)
    region_id: str = Field(min_length=1, max_length=128)


class SiteRegionRead(SiteRegionUpdate):
    """Serialize a persisted tenant/site region assignment.

    Extends SiteRegionUpdate with the stored assignment identifier. Validation
    follows the parent model and may raise Pydantic validation errors.
    """

    id: str


class PlacementRead(BaseModel):
    """Serialize one camera-role placement and its fencing authority state.

    Fields expose owner node, generation, cleanup obligations, lease/autonomy
    windows and assignment time. Validation may raise for incompatible input.
    """

    camera_id: str
    role: str
    region_id: str
    node_id: str
    cleanup_node_ids: list[str] = Field(default_factory=list)
    generation: int
    applied_generation: int | None = None
    active: bool
    reason: str
    lease_expires_at: datetime
    autonomy_expires_at: datetime | None = None
    assigned_at: datetime


class PlacementRunRead(BaseModel):
    """Serialize the outcome of one bounded placement-controller run.

    Fields report scanned, moved, unplaced and autonomy-deferred counts plus the
    continuation cursor. Validation may raise for incompatible input types.
    """

    scanned: int
    moved: int
    unplaced: int
    deferred_autonomy: int = 0
    cursor: str | None = None



class FenceAssignmentRead(BaseModel):
    """Serialize active fencing authority granted to a node assignment.

    Fields include assignment identity, camera role, generation, authority
    deadlines and execution key. Validation may raise for incompatible input.
    """

    assignment_id: str
    camera_id: str
    role: NodeRole
    generation: int
    lease_expires_at: datetime
    autonomy_expires_at: datetime | None = None
    execution_key: str
    execution_keys: list[str] = Field(default_factory=list, max_length=8)


class FenceRevocationRead(BaseModel):
    """Serialize durable revocation evidence sent to a node.

    Fields identify the revoked generation, execution key and creation time.
    Validation may raise for incompatible input types.
    """

    revocation_id: str
    assignment_id: str
    camera_id: str
    role: NodeRole
    revoked_generation: int
    execution_key: str
    execution_keys: list[str] = Field(default_factory=list, max_length=8)
    created_at: datetime


class NodeFenceSnapshotRead(BaseModel):
    """Serialize the fencing snapshot delivered to one infrastructure node.

    Fields contain server time, node identity, active assignments and pending
    revocations. Validation may raise for incompatible nested input.
    """

    server_time: datetime
    node_id: str
    assignments: list[FenceAssignmentRead] = Field(default_factory=list)
    revocations: list[FenceRevocationRead] = Field(default_factory=list)


class RevocationAckRead(BaseModel):
    """Serialize acknowledgement state for one placement revocation.

    Fields return the revocation identifier and acknowledgement result.
    Validation may raise for incompatible input types.
    """

    revocation_id: str
    acknowledged: bool
