import asyncio
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services.mediamtx import MediaMTXClient
from app.services.playback import PlaybackClient


class NodeEndpointError(RuntimeError):
    """Raised when an infrastructure-node media/playback endpoint is unusable."""


def validate_node_endpoint(value: str, *, field: str) -> str:
    """Validate a credential-free HTTP/HTTPS infrastructure-node endpoint.

    Args:
        value: Candidate endpoint URL.
        field: Endpoint field name used in validation errors.

    Returns:
        Validated URL without a trailing slash.

    Raises:
        NodeEndpointError: If scheme/host is invalid or credentials/query/fragment
            are embedded in the endpoint.
    """
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NodeEndpointError(f"{field} must be an http/https URL with a host")
    if parsed.username or parsed.password:
        raise NodeEndpointError(f"{field} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise NodeEndpointError(f"{field} must not contain query/fragment")
    return value.rstrip("/")


def endpoint(node: InfrastructureNodeEntity, key: str, fallback: str | None = None) -> str:
    """Resolve and validate one named endpoint from an infrastructure node.

    Args:
        node: Infrastructure node entity.
        key: Endpoint key stored in the node metadata.
        fallback: Optional endpoint used when the node omits the key.

    Returns:
        Validated endpoint URL.

    Raises:
        NodeEndpointError: If no endpoint is available or validation fails.
    """
    value = (node.endpoints_json or {}).get(key)
    if value:
        return validate_node_endpoint(str(value), field=key)
    if fallback:
        return validate_node_endpoint(fallback, field=key)
    raise NodeEndpointError(f"node {node.id} has no {key} endpoint")


class NodeClientPool:
    """Cache media and playback clients by validated infrastructure endpoint."""

    def __init__(self):
        self._media: dict[str, MediaMTXClient] = {}
        self._playback: dict[str, PlaybackClient] = {}
        self._lock = asyncio.Lock()

    async def media(self, node: InfrastructureNodeEntity) -> MediaMTXClient:
        """Return a cached MediaMTX client for an infrastructure node.

        Args:
            node: Infrastructure node providing the media role.

        Returns:
            MediaMTXClient bound to the node API endpoint.

        Raises:
            NodeEndpointError: If the node has no usable media API endpoint.
        """
        fallback = settings.mediamtx_api_url if node.id == settings.placement_local_node_id else None
        base = endpoint(node, "api_url", fallback)
        async with self._lock:
            client = self._media.get(base)
            if client is None:
                client = MediaMTXClient(base)
                self._media[base] = client
            return client

    async def playback(self, node: InfrastructureNodeEntity) -> PlaybackClient:
        """Return a cached playback client for an infrastructure node.

        Args:
            node: Infrastructure node providing playback access.

        Returns:
            PlaybackClient bound to the node internal playback endpoint.

        Raises:
            NodeEndpointError: If the node has no usable playback endpoint.
        """
        fallback = (
            settings.mediamtx_playback_internal_url
            if node.id == settings.placement_local_node_id
            else None
        )
        base = endpoint(node, "playback_internal_url", fallback)
        async with self._lock:
            client = self._playback.get(base)
            if client is None:
                client = PlaybackClient(base)
                self._playback[base] = client
            return client


async def get_node(
    session: AsyncSession,
    node_id: str,
    *,
    required_role: str | None = None,
) -> InfrastructureNodeEntity:
    """Load one enabled infrastructure node and optionally require a role.

    Args:
        session: Database session used to load node state.
        node_id: Infrastructure node identifier.
        required_role: Optional role the node must advertise.

    Returns:
        Enabled InfrastructureNodeEntity.

    Raises:
        NodeEndpointError: If the node is absent, disabled, or lacks the role.
    """
    node = await session.get(InfrastructureNodeEntity, node_id)
    if node is None or not node.enabled:
        raise NodeEndpointError(f"infrastructure node {node_id} is unavailable")
    if required_role and required_role not in set(node.roles_json or []):
        raise NodeEndpointError(f"node {node_id} does not provide role {required_role}")
    return node


node_clients = NodeClientPool()


async def assigned_node(
    session: AsyncSession,
    camera_id: str,
    role: str,
    *,
    require_live_lease: bool = True,
) -> tuple[PlacementAssignmentEntity, InfrastructureNodeEntity]:
    """Resolve the active placement assignment and infrastructure node for a camera.

    Args:
        session: Database session used to read placement and node state.
        camera_id: Camera whose assignment is required.
        role: Placement role such as media or recording.
        require_live_lease: Whether an expired lease must be rejected.

    Returns:
        Tuple of active PlacementAssignmentEntity and its enabled node.

    Raises:
        NodeEndpointError: If assignment/node/role/lease validation fails.
    """
    row = (
        await session.execute(
            select(PlacementAssignmentEntity).where(
                PlacementAssignmentEntity.camera_id == camera_id,
                PlacementAssignmentEntity.role == role,
            )
        )
    ).scalar_one_or_none()
    if row is None or not row.active:
        raise NodeEndpointError(f"camera {camera_id} has no active {role} assignment")
    if require_live_lease:
        expiry = row.lease_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            raise NodeEndpointError(f"camera {camera_id} {role} assignment lease expired")
    node = await get_node(session, row.node_id, required_role=role)
    return row, node


def public_media_bases(node: InfrastructureNodeEntity) -> tuple[str, str]:
    """Return validated public WebRTC and HLS bases for one media node.

    Args:
        node: Infrastructure node serving public media.

    Returns:
        Tuple of WebRTC and HLS public base URLs.

    Raises:
        NodeEndpointError: If either public endpoint is unavailable or invalid.
    """
    local = node.id == settings.placement_local_node_id
    webrtc = endpoint(
        node,
        "webrtc_public_base",
        settings.mediamtx_webrtc_public_base if local else None,
    )
    hls = endpoint(
        node,
        "hls_public_base",
        settings.mediamtx_hls_public_base if local else None,
    )
    return webrtc, hls
