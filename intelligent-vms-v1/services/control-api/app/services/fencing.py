from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.models.placement import PlacementAssignmentEntity, PlacementRevocationEntity
from app.services.recording import make_record_stream_key
from app.services.stream_keys import make_role_stream_key


class FenceSnapshotTooLarge(RuntimeError):
    """Raised when a node fencing snapshot exceeds the configured safety bound."""


class FenceConflict(RuntimeError):
    """Raised when a fencing/revocation operation conflicts with current ownership."""


def _execution_keys(
    camera: CameraEntity,
    role: str,
    recording_policy: RecordingPolicyEntity | None,
) -> list[str]:
    if role == "media":
        keys = [camera.stream_key]
        if camera.sub_path:
            keys.append(make_role_stream_key(camera.stream_key, "main"))
        if camera.third_stream_key:
            keys.append(camera.third_stream_key)
        return keys
    if role == "recording":
        if recording_policy is not None:
            return [recording_policy.record_stream_key]
        return [make_record_stream_key(camera.stream_key)]
    if role == "ai":
        return [f"ai:{camera.id}"]
    raise FenceConflict(f"unsupported placement role {role}")


async def fence_snapshot(session: AsyncSession, node_id: str) -> dict:
    """Build the bounded active-assignment/revocation snapshot for one node.

    Args:
        session: Database session used to read placement and camera state.
        node_id: Infrastructure node receiving the fencing snapshot.

    Returns:
        Snapshot dictionary containing server time, assignments, and revocations.

    Raises:
        FenceSnapshotTooLarge: If assignments or pending revocations exceed the
            configured per-node snapshot bound.
        FenceConflict: If an assignment role cannot be mapped to an execution key.
        Exception: Database failures propagate to the caller.
    """
    limit = max(1, settings.node_fence_snapshot_max_items)

    assignments = list(
        (
            await session.execute(
                select(PlacementAssignmentEntity)
                .where(
                    PlacementAssignmentEntity.node_id == node_id,
                    PlacementAssignmentEntity.active.is_(True),
                )
                .order_by(PlacementAssignmentEntity.id)
                .limit(limit + 1)
            )
        ).scalars().all()
    )
    revocations = list(
        (
            await session.execute(
                select(PlacementRevocationEntity)
                .where(
                    PlacementRevocationEntity.node_id == node_id,
                    PlacementRevocationEntity.acknowledged_at.is_(None),
                    PlacementRevocationEntity.cancelled_at.is_(None),
                )
                .order_by(PlacementRevocationEntity.id)
                .limit(limit + 1)
            )
        ).scalars().all()
    )

    if len(assignments) > limit or len(revocations) > limit:
        raise FenceSnapshotTooLarge(
            f"node fence snapshot exceeds configured limit {limit}"
        )

    camera_ids = {
        row.camera_id for row in assignments
    } | {
        row.camera_id for row in revocations
    }

    cameras: dict[str, CameraEntity] = {}
    recording_policies: dict[str, RecordingPolicyEntity] = {}
    if camera_ids:
        camera_rows = (
            await session.execute(
                select(CameraEntity).where(CameraEntity.id.in_(camera_ids))
            )
        ).scalars().all()
        cameras = {row.id: row for row in camera_rows}

        policy_rows = (
            await session.execute(
                select(RecordingPolicyEntity).where(
                    RecordingPolicyEntity.camera_id.in_(camera_ids)
                )
            )
        ).scalars().all()
        recording_policies = {row.camera_id: row for row in policy_rows}

    active_payload = []
    for row in assignments:
        camera = cameras.get(row.camera_id)
        if camera is None:
            continue
        active_payload.append(
            {
                "assignment_id": row.id,
                "camera_id": row.camera_id,
                "role": row.role,
                "generation": row.generation,
                "lease_expires_at": row.lease_expires_at,
                "autonomy_expires_at": row.autonomy_expires_at,
                "execution_key": _execution_keys(
                    camera,
                    row.role,
                    recording_policies.get(row.camera_id),
                )[0],
                "execution_keys": _execution_keys(
                    camera,
                    row.role,
                    recording_policies.get(row.camera_id),
                ),
            }
        )

    revoke_payload = []
    for row in revocations:
        camera = cameras.get(row.camera_id)
        if camera is None:
            continue
        execution_keys = list(row.execution_keys_json or [])
        if not execution_keys:
            execution_keys = _execution_keys(
                camera,
                row.role,
                recording_policies.get(row.camera_id),
            )
        revoke_payload.append(
            {
                "revocation_id": row.id,
                "assignment_id": row.assignment_id,
                "camera_id": row.camera_id,
                "role": row.role,
                "revoked_generation": row.revoked_generation,
                "execution_key": execution_keys[0],
                "execution_keys": execution_keys,
                "created_at": row.created_at,
            }
        )

    return {
        "server_time": datetime.now(timezone.utc),
        "node_id": node_id,
        "assignments": active_payload,
        "revocations": revoke_payload,
    }


async def acknowledge_revocation(
    session: AsyncSession,
    node_id: str,
    revocation_id: str,
) -> bool:
    """Acknowledge one durable revocation after the old execution is removed.

    Args:
        session: Database session used to update revocation/assignment state.
        node_id: Node claiming completion of the revocation.
        revocation_id: Durable revocation identifier.

    Returns:
        True when the revocation is already or newly acknowledged; False when it
        is absent, belongs to another node, or has been cancelled.

    Raises:
        FenceConflict: If the node is still the current active owner.
        Exception: Database failures propagate to the caller.
    """
    row = await session.get(PlacementRevocationEntity, revocation_id)
    if row is None or row.node_id != node_id:
        return False
    if row.cancelled_at is not None:
        return False
    if row.acknowledged_at is not None:
        return True

    assignment = await session.get(PlacementAssignmentEntity, row.assignment_id)
    if assignment is not None and assignment.node_id == node_id and assignment.active:
        raise FenceConflict("cannot acknowledge revocation for the current owner")

    row.acknowledged_at = datetime.now(timezone.utc)

    if assignment is not None:
        cleanup = [
            old_node_id
            for old_node_id in (assignment.cleanup_node_ids_json or [])
            if old_node_id != node_id
        ]
        assignment.cleanup_node_ids_json = cleanup

    return True
