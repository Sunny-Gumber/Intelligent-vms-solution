import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Send

from app.core.auth import Principal, require_roles
from app.core.config import settings
from app.core.errors import build_error_payload
from app.db.session import get_session
from app.models.entities import CameraEntity, ManualRecordingSessionEntity, RecordingPolicyEntity
from app.models.placement import PlacementAssignmentEntity, PlacementRevocationEntity
from app.models.schemas import RecordingPolicyRead, RecordingPolicyUpdate, RecordingTimespan
from app.routers.cameras import authorized_camera
from app.services.coordination import PlacementExecutionBusy, require_placement_execution_lock
from app.services.outbox import enqueue_message_once
from app.services.mediamtx import mediamtx
from app.services.node_media import NodeEndpointError, assigned_node, get_node, node_clients
from app.services.playback import (
    UPSTREAM_FAILURE_DETAIL,
    UPSTREAM_TIMEOUT_DETAIL,
    PlaybackError,
    playback_client,
)
from app.services.recording import make_record_stream_key, provision_recording
from app.services.recording_health import (
    record_segment_completion,
    sync_recording_health_policy,
)
from app.services.recording_index import RecordingIndexError, recording_index
from app.services.search_page import apply_page_headers

router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])
internal_router = APIRouter(prefix="/internal/v1/recording", tags=["internal-recording"])
log = logging.getLogger(__name__)
_export_slots = asyncio.Semaphore(settings.recording_export_max_concurrent_per_process)

_DURATION_RE = re.compile(r"(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m)?(?:(?P<s>\d+(?:\.\d+)?)s)?$")
# MediaMTX v1.21.1 internal/conf/path.go: "maximum segment duration is 1 day".
_MAX_SEGMENT_DURATION_SECONDS = 24 * 60 * 60
# %s is exactly 10 digits and %f is exactly 6 digits in internal/recordstore/path.go.
_SEGMENT_NAME_RE = re.compile(r"^(?P<seconds>[0-9]{10})(?:-(?P<micros>[0-9]{6}))?$")


def _recording_valid_until(assignment: PlacementAssignmentEntity) -> datetime:
    deadline = assignment.lease_expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    autonomy = assignment.autonomy_expires_at
    if autonomy is not None:
        if autonomy.tzinfo is None:
            autonomy = autonomy.replace(tzinfo=timezone.utc)
        if autonomy > deadline:
            deadline = autonomy
    return deadline


def _validate_recording_fence(
    assignment: PlacementAssignmentEntity | None,
    recording_node_id: str | None,
    assignment_generation: int | None,
    *,
    now: datetime | None = None,
    evidence_time: datetime | None = None,
) -> None:
    if assignment is None or not assignment.active:
        raise HTTPException(409, "Recording assignment is not active")
    if recording_node_id is None or assignment_generation is None:
        raise HTTPException(422, "Distributed recording hook is missing fencing identity")

    if (
        recording_node_id != assignment.node_id
        or int(assignment_generation) != assignment.generation
    ):
        raise HTTPException(409, "Recording segment came from a stale assignment generation")

    effective_time = evidence_time or now or datetime.now(timezone.utc)
    if effective_time.tzinfo is None:
        effective_time = effective_time.replace(tzinfo=timezone.utc)
    if effective_time > _recording_valid_until(assignment):
        raise HTTPException(409, "Recording segment is outside the assignment authority window")


async def _validate_recording_evidence(
    session: AsyncSession,
    *,
    camera_id: str,
    recording_node_id: str | None,
    assignment_generation: int | None,
    evidence_time: datetime,
) -> None:
    assignment = (
        await session.execute(
            select(PlacementAssignmentEntity).where(
                PlacementAssignmentEntity.camera_id == camera_id,
                PlacementAssignmentEntity.role == "recording",
            )
        )
    ).scalar_one_or_none()

    if (
        assignment is not None
        and recording_node_id == assignment.node_id
        and assignment_generation == assignment.generation
    ):
        _validate_recording_fence(
            assignment,
            recording_node_id,
            assignment_generation,
            evidence_time=evidence_time,
        )
        return

    if recording_node_id is None or assignment_generation is None:
        raise HTTPException(422, "Distributed recording hook is missing fencing identity")

    revocation = (
        await session.execute(
            select(PlacementRevocationEntity).where(
                PlacementRevocationEntity.camera_id == camera_id,
                PlacementRevocationEntity.role == "recording",
                PlacementRevocationEntity.node_id == recording_node_id,
                PlacementRevocationEntity.revoked_generation == assignment_generation,
            )
        )
    ).scalar_one_or_none()
    if revocation is None or revocation.valid_until is None:
        raise HTTPException(409, "Recording segment came from an unauthorized generation")

    valid_until = revocation.valid_until
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)
    if evidence_time.tzinfo is None:
        evidence_time = evidence_time.replace(tzinfo=timezone.utc)
    if evidence_time > valid_until:
        raise HTTPException(409, "Recording segment completed after revoked generation authority ended")


def _policy_read(row: RecordingPolicyEntity) -> RecordingPolicyRead:
    return RecordingPolicyRead(
        camera_id=row.camera_id,
        mode=row.mode,
        enabled=row.enabled,
        record_stream_key=row.record_stream_key,
        recording_node_id=row.recording_node_id,
        retention_days=row.retention_days,
        part_duration_ms=row.part_duration_ms,
        segment_duration_seconds=row.segment_duration_seconds,
        max_part_size_mb=row.max_part_size_mb,
        updated_at=row.updated_at,
    )


async def _policy_for_camera(
    session: AsyncSession,
    camera_id: str,
    *,
    lock: bool = False,
) -> RecordingPolicyEntity | None:
    stmt = select(RecordingPolicyEntity).where(RecordingPolicyEntity.camera_id == camera_id)
    if lock:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _cleanup_created_recording_path(camera_id: str, stream_key: str) -> None:
    try:
        await mediamtx.delete_path(stream_key)
    except Exception as cleanup_exc:
        # Cleanup is secondary to the original policy failure. Avoid logging
        # exception text because upstream errors may contain endpoint details.
        log.warning(
            "recording_policy_cleanup_failed camera_id=%s error_type=%s",
            camera_id,
            cleanup_exc.__class__.__name__,
        )


async def _playback_for_policy(session: AsyncSession, policy: RecordingPolicyEntity):
    if not settings.placement_execution_enabled:
        return playback_client
    try:
        node = await get_node(session, policy.recording_node_id, required_role="recording")
        return await node_clients.playback(node)
    except NodeEndpointError as exc:
        raise HTTPException(503, f"Recording node unavailable: {exc}") from exc


def _playback_timespans(
    items: list[dict],
    start: datetime,
    end: datetime,
) -> list[RecordingTimespan]:
    """Normalize untrusted playback-service items into bounded aware timespans."""
    spans: list[RecordingTimespan] = []
    for item in items:
        try:
            span_start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
            duration = float(item["duration"])
            if span_start.tzinfo is None or not math.isfinite(duration) or duration <= 0:
                continue
            span_end = span_start + timedelta(seconds=duration)
            clipped_start = max(span_start, start)
            clipped_end = min(span_end, end)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if clipped_end <= clipped_start:
            continue
        spans.append(
            RecordingTimespan(
                start=clipped_start,
                duration=(clipped_end - clipped_start).total_seconds(),
                end=clipped_end,
            )
        )
    return sorted(spans, key=lambda span: span.start)


def _bounded_playback_duration(
    spans: list[RecordingTimespan],
    start: datetime,
    requested_duration: float,
) -> float | None:
    """Bound playback to actual continuous coverage beginning at start."""
    requested_end = start + timedelta(seconds=requested_duration)
    cursor = start
    has_coverage = False
    for span in sorted(spans, key=lambda item: item.start):
        if span.end <= cursor:
            continue
        if span.start > cursor:
            break
        has_coverage = True
        cursor = max(cursor, span.end)
        if cursor >= requested_end:
            return requested_duration
    if not has_coverage or cursor <= start:
        return None
    return max(0.001, (cursor - start).total_seconds())


@router.get("/cameras/{camera_id}/policy", response_model=RecordingPolicyRead)
async def get_policy(
    camera_id: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return the persisted recording policy for an authorized camera.

    Args:
        camera_id: Camera identifier whose policy is requested.
        session: Database session used for authorization/policy lookup.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        RecordingPolicyRead for the camera.

    Raises:
        HTTPException: HTTP 404 when camera/policy is unavailable or out of scope.
    """
    await authorized_camera(session, camera_id, principal)
    row = await _policy_for_camera(session, camera_id)
    if not row:
        raise HTTPException(404, "Recording policy not configured")
    return _policy_read(row)


@router.put("/cameras/{camera_id}/policy", response_model=RecordingPolicyRead)
async def put_policy(
    camera_id: str,
    payload: RecordingPolicyUpdate,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Create or update a camera recording policy and apply it safely.

    Args:
        camera_id: Camera whose recording policy is being changed.
        payload: Validated policy settings supplied by the caller.
        session: Database session used for policy and placement state.
        principal: Authorized VMS operator or administrator.

    Returns:
        The persisted recording policy after the change is committed.

    Raises:
        HTTPException: If authorization, validation, placement, or recorder
            application prevents the requested policy from being applied safely.
    """
    camera = await authorized_camera(session, camera_id, principal)

    if settings.placement_execution_enabled:
        # Serialize immediate recorder start/stop with ownership generation changes.
        # A busy fence is retriable and safer than applying to a stale node.
        try:
            await require_placement_execution_lock(session)
        except PlacementExecutionBusy as exc:
            raise HTTPException(
                409,
                "Placement ownership is changing; retry recording policy update",
            ) from exc

    if payload.enabled and payload.mode in {"event", "scheduled"}:
        raise HTTPException(
            422,
            {"code": "MODE_NOT_IMPLEMENTED", "message": "Event/scheduled recording is reserved for a later phase"},
        )

    row = await _policy_for_camera(session, camera_id, lock=True)
    created = row is None
    was_enabled = bool(row and row.enabled)
    if not payload.enabled or payload.mode == "disabled":
        now = datetime.now(timezone.utc)
        active_manual = (
            await session.execute(
                select(ManualRecordingSessionEntity.id).where(
                    ManualRecordingSessionEntity.camera_id == camera.id,
                    ManualRecordingSessionEntity.state == "ACTIVE",
                    ManualRecordingSessionEntity.max_stop_at > now,
                ).limit(1)
            )
        ).scalar_one_or_none()
        if active_manual is not None:
            raise HTTPException(
                409,
                "Stop active manual recording sessions before disabling continuous recording",
            )
    if row is None:
        row = RecordingPolicyEntity(
            camera_id=camera.id,
            record_stream_key=make_record_stream_key(camera.stream_key),
            recording_node_id=camera.media_node_id,
        )
        session.add(row)

    row.mode = payload.mode
    row.enabled = bool(payload.enabled and payload.mode != "disabled")
    row.retention_days = payload.retention_days
    row.part_duration_ms = payload.part_duration_ms
    row.segment_duration_seconds = payload.segment_duration_seconds
    row.max_part_size_mb = payload.max_part_size_mb

    await session.flush()
    # Preserve rollback-cleanup identifiers while the ORM row is still loaded.
    # AsyncSession.rollback() expires ORM state; reading row attributes after
    # rollback can trigger implicit async I/O and raise MissingGreenlet.
    cleanup_camera_id = camera.id
    cleanup_record_stream_key = row.record_stream_key
    try:
        if not settings.placement_execution_enabled:
            await provision_recording(camera, row)
        elif row.enabled:
            # Apply immediately when an assignment already exists. Newly-enabled
            # policies without placement are committed and picked up by the
            # placement controller + distributed reconciler.
            try:
                assignment, node = await assigned_node(session, camera.id, "recording")
                client = await node_clients.media(node)
                await provision_recording(
                    camera,
                    row,
                    client=client,
                    recording_node_id=assignment.node_id,
                    assignment_generation=assignment.generation,
                )
                row.recording_node_id = assignment.node_id
                assignment.applied_generation = assignment.generation
            except NodeEndpointError as exc:
                log.warning(
                    "recording_policy_apply_deferred camera_id=%s reason=%s",
                    camera.id,
                    exc.__class__.__name__,
                )
        elif was_enabled:
            # Disabling recording must stop the current recorder before policy
            # state is committed, otherwise an unreachable node could continue
            # writing indefinitely.
            node = await get_node(session, row.recording_node_id, required_role="recording")
            client = await node_clients.media(node)
            await client.delete_path(row.record_stream_key)

        await sync_recording_health_policy(
            session,
            row,
            was_enabled=was_enabled,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        if created and not settings.placement_execution_enabled:
            await _cleanup_created_recording_path(cleanup_camera_id, cleanup_record_stream_key)
        raise HTTPException(502, f"Recording node could not apply policy: {exc}") from exc

    await session.refresh(row)
    return _policy_read(row)


@router.get("/cameras/{camera_id}/timeline", response_model=list[RecordingTimespan])
async def timeline(
    response: Response,
    camera_id: str,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Return bounded playable recording spans for an authorized camera.

    Args:
        camera_id: Camera identifier to query.
        start: Optional timezone-aware interval start.
        end: Optional timezone-aware interval end.
        session: Database session used for authorization and recorder ownership.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        Chronologically sorted recording timespans clipped to the request window.
        The JSON body stays an array. ``X-VMS-Partial`` and ``X-VMS-Skipped-Rows``
        report malformed recording-index rows skipped while filling it.

    Raises:
        HTTPException: If authorization/policy fails, the interval is invalid or
            recording-index/playback services are unavailable.
    """
    camera = await authorized_camera(session, camera_id, principal)
    policy = await _policy_for_camera(session, camera_id)
    if not policy:
        raise HTTPException(404, "Recording policy not configured")
    now = datetime.now(timezone.utc)
    end = end or now
    start = start or (end - timedelta(hours=24))
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "start and end must include a timezone")
    if end <= start:
        raise HTTPException(422, "end must be after start")
    if end - start > timedelta(hours=max(1, settings.recording_query_max_window_hours)):
        raise HTTPException(422, "recording timeline window exceeds configured maximum")

    if settings.placement_execution_enabled:
        try:
            rows = await recording_index.segments(
                tenant_id=camera.tenant_id,
                site_id=camera.site_id,
                camera_id=camera.id,
                start=start,
                end=end,
                limit=settings.recording_query_max_segments,
            )
        except RecordingIndexError as exc:
            raise HTTPException(503, str(exc)) from exc
        apply_page_headers(response, rows)
        indexed = [
            RecordingTimespan(
                start=max(row["segment_start"], start),
                duration=(
                    min(row["segment_end"], end) - max(row["segment_start"], start)
                ).total_seconds(),
                end=min(row["segment_end"], end),
            )
            for row in rows
        ]

        # The current MediaMTX segment may not have emitted its completion hook
        # yet. Include current-node spans that do not overlap already indexed
        # history so operators can play recent footage without waiting for a
        # long segment to close.
        try:
            current_playback = await _playback_for_policy(session, policy)
            current_items = await current_playback.list_timespans(
                policy.record_stream_key, start, end
            )
        except (HTTPException, PlaybackError):
            current_items = []

        recent = []
        for span in _playback_timespans(current_items, start, end):
            overlaps_index = any(
                existing.start < span.end and existing.end > span.start
                for existing in indexed
            )
            if not overlaps_index:
                recent.append(span)
        return sorted(indexed + recent, key=lambda span: span.start)

    apply_page_headers(response, None)
    client = await _playback_for_policy(session, policy)
    try:
        spans = await client.list_timespans(policy.record_stream_key, start, end)
    except PlaybackError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    return _playback_timespans(spans, start, end)


class _PlaybackProxyResponse(StreamingResponse):
    """Proxy a playback body, or return 504/502 if it stalls before the first byte.

    Starlette sends response headers before iterating a normal streaming body.
    This waits for the first upstream chunk so a recorder that has already
    returned headers and then goes silent is still a gateway error. A stall
    after bytes have started ends the body and relies on the generator to
    close the upstream response and client.
    """

    async def stream_response(self, send: Send) -> None:
        iterator = self.body_iterator.__aiter__()
        try:
            try:
                first = await anext(iterator)
            except StopAsyncIteration:
                await _send_start(send, self.status_code, self.raw_headers)
                await _send_end(send)
                return
            except httpx.TimeoutException:
                await _send_gateway_error(send, 504, UPSTREAM_TIMEOUT_DETAIL)
                return
            except httpx.HTTPError:
                await _send_gateway_error(send, 502, UPSTREAM_FAILURE_DETAIL)
                return

            await _send_start(send, self.status_code, self.raw_headers)
            if first:
                await _send_chunk(send, _as_bytes(first, self.charset))
            try:
                async for chunk in iterator:
                    await _send_chunk(send, _as_bytes(chunk, self.charset))
            except (httpx.TimeoutException, httpx.HTTPError):
                await _send_end(send)
                return
            await _send_end(send)
        finally:
            await iterator.aclose()


def _as_bytes(chunk: bytes | memoryview | str, charset: str) -> bytes:
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, memoryview):
        return chunk.tobytes()
    return chunk.encode(charset)


async def _send_start(send: Send, status: int, headers: list[tuple[bytes, bytes]]) -> None:
    await send({"type": "http.response.start", "status": status, "headers": headers})


async def _send_chunk(send: Send, chunk: bytes) -> None:
    await send({"type": "http.response.body", "body": chunk, "more_body": True})


async def _send_end(send: Send) -> None:
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _send_gateway_error(send: Send, status: int, message: str) -> None:
    body = json.dumps(build_error_payload(status, message)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


@router.get("/cameras/{camera_id}/play")
async def play(
    request: Request,
    camera_id: str,
    start: datetime = Query(...),
    duration: float = Query(..., gt=0.0, le=14400.0),
    format: Literal["fmp4", "mp4"] = Query(default="fmp4"),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator", "viewer")),
):
    """Stream bounded recording playback from the responsible recorder node.

    Args:
        request: Incoming HTTP request, including optional Range header.
        camera_id: Camera identifier to play.
        start: Timezone-aware playback start.
        duration: Requested playback duration in seconds.
        format: Playback container format.
        session: Database session for authorization/index ownership lookup.
        principal: Authenticated administrator, operator or viewer.

    Returns:
        StreamingResponse proxied from the selected playback service. A stall
        before the first playback byte is HTTP 504 instead of an open stream.

    Raises:
        HTTPException: If authorization, policy, index, node, or playback
            validation fails, or if the upstream stalls before streaming starts.
    """
    camera = await authorized_camera(session, camera_id, principal)
    if start.tzinfo is None:
        raise HTTPException(422, "start must include a timezone")
    now = datetime.now(timezone.utc)
    if start > now:
        raise HTTPException(422, "playback start must not be in the future")
    policy = await _policy_for_camera(session, camera_id)
    if not policy:
        raise HTTPException(404, "Recording policy not configured")

    requested_end = start + timedelta(seconds=duration)
    play_duration = duration
    if settings.placement_execution_enabled:
        try:
            segment = await recording_index.segment_for_start(
                tenant_id=camera.tenant_id,
                site_id=camera.site_id,
                camera_id=camera.id,
                start=start,
            )
        except RecordingIndexError as exc:
            raise HTTPException(503, str(exc)) from exc
        if segment is None:
            # A currently-open segment is intentionally absent from the completed
            # index. Validate current-node coverage before asking it to stream.
            playback = await _playback_for_policy(session, policy)
            try:
                current_items = await playback.list_timespans(
                    policy.record_stream_key,
                    start,
                    requested_end,
                )
            except PlaybackError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc
            play_duration = _bounded_playback_duration(
                _playback_timespans(current_items, start, requested_end),
                start,
                duration,
            )
            if play_duration is None:
                raise HTTPException(404, "No recording is available at requested start")
        else:
            segment_start = segment["segment_start"]
            segment_end = segment["segment_end"]
            if not (segment_start <= start < segment_end):
                raise HTTPException(404, "No recording is available at requested start")
            try:
                node = await get_node(
                    session,
                    str(segment["recording_node_id"]),
                    required_role="recording",
                )
                playback = await node_clients.playback(node)
            except NodeEndpointError as exc:
                raise HTTPException(503, f"Historical recording node unavailable: {exc}") from exc
            # Never stream one request across a recording-node/failover boundary.
            play_duration = min(
                duration,
                max(0.001, (segment_end - start).total_seconds()),
            )
    else:
        playback = await _playback_for_policy(session, policy)
        try:
            items = await playback.list_timespans(
                policy.record_stream_key,
                start,
                requested_end,
            )
        except PlaybackError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        play_duration = _bounded_playback_duration(
            _playback_timespans(items, start, requested_end),
            start,
            duration,
        )
        if play_duration is None:
            raise HTTPException(404, "No recording is available at requested start")

    # Ordinary play omits timeout_seconds. open_stream then applies
    # recording_playback_connect_timeout_seconds and
    # recording_playback_read_timeout_seconds. Export passes
    # recording_export_io_timeout_seconds and keeps that single-phase contract.
    try:
        client, upstream = await playback.open_stream(
            policy.record_stream_key,
            start,
            play_duration,
            format,
            request.headers.get("range"),
        )
    except PlaybackError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass

    headers = {}
    for name in ("content-length", "content-range", "accept-ranges", "cache-control"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]
    return _PlaybackProxyResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=headers,
    )


def _coverage_is_continuous(spans: list[RecordingTimespan], start: datetime, end: datetime) -> bool:
    cursor = start
    for span in sorted(spans, key=lambda item: item.start):
        if span.end <= cursor:
            continue
        if span.start > cursor:
            return False
        cursor = max(cursor, span.end)
        if cursor >= end:
            return True
    return False


def _clip_filename(camera_id: str, start: datetime, end: datetime) -> str:
    safe_camera = re.sub(r"[^A-Za-z0-9_-]", "_", camera_id)[:80] or "camera"
    start_text = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end_text = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"clip-{safe_camera}-{start_text}-{end_text}.mp4"


async def stream_recording_clip(
    request: Request,
    camera: CameraEntity,
    start: datetime,
    duration: float,
    session: AsyncSession,
):
    """Stream one validated authoritative recording interval as MP4."""
    if start.tzinfo is None:
        raise HTTPException(422, "start must include a timezone")
    if duration <= 0 or duration > settings.recording_export_max_duration_seconds:
        raise HTTPException(422, "clip duration exceeds configured export maximum")
    end = start + timedelta(seconds=duration)
    now = datetime.now(timezone.utc)
    if start > now or end > now:
        raise HTTPException(422, "clip interval must not extend into the future")
    policy = await _policy_for_camera(session, camera.id)
    if not policy:
        raise HTTPException(404, "Recording policy not configured")

    playback = None
    spans: list[RecordingTimespan] = []
    if settings.placement_execution_enabled:
        try:
            rows = await recording_index.segments(
                tenant_id=camera.tenant_id, site_id=camera.site_id, camera_id=camera.id,
                start=start, end=end, limit=settings.recording_query_max_segments,
            )
        except RecordingIndexError as exc:
            raise HTTPException(503, "Recording index unavailable") from exc
        nodes = {str(row["recording_node_id"]) for row in rows if row.get("recording_node_id")}
        spans = [
            RecordingTimespan(start=max(row["segment_start"], start),
                duration=(min(row["segment_end"], end)-max(row["segment_start"], start)).total_seconds(),
                end=min(row["segment_end"], end))
            for row in rows if min(row["segment_end"], end) > max(row["segment_start"], start)
        ]
        if not _coverage_is_continuous(spans, start, end):
            raise HTTPException(409, "Requested clip does not have continuous finalized recording coverage")
        if len(nodes) != 1:
            raise HTTPException(409, "Requested clip crosses a recording-node boundary")
        try:
            node = await get_node(session, next(iter(nodes)), required_role="recording")
            playback = await node_clients.playback(node)
        except NodeEndpointError as exc:
            raise HTTPException(503, "Recording node unavailable") from exc
    else:
        playback = await _playback_for_policy(session, policy)
        try:
            items = await playback.list_timespans(policy.record_stream_key, start, end)
        except PlaybackError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        for item in items:
            try:
                span_start = datetime.fromisoformat(str(item["start"]).replace("Z", "+00:00"))
                span_end = span_start + timedelta(seconds=float(item["duration"]))
            except (KeyError, TypeError, ValueError):
                continue
            clipped_start, clipped_end = max(span_start, start), min(span_end, end)
            if clipped_end > clipped_start:
                spans.append(RecordingTimespan(start=clipped_start, duration=(clipped_end-clipped_start).total_seconds(), end=clipped_end))
        if not _coverage_is_continuous(spans, start, end):
            raise HTTPException(409, "Requested clip does not have continuous recording coverage")

    try:
        await asyncio.wait_for(_export_slots.acquire(), timeout=0.05)
    except TimeoutError as exc:
        raise HTTPException(429, "Recording export concurrency limit reached") from exc
    try:
        client, upstream = await playback.open_stream(
            policy.record_stream_key, start, duration, "mp4", request.headers.get("range"),
            settings.recording_export_io_timeout_seconds,
        )
    except PlaybackError as exc:
        _export_slots.release()
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception:
        _export_slots.release()
        raise

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()
            _export_slots.release()

    headers = {
        "Content-Disposition": f'attachment; filename="{_clip_filename(camera.id, start, end)}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
    }
    for name in ("content-length", "content-range", "accept-ranges"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]
    return StreamingResponse(body(), status_code=upstream.status_code, media_type="video/mp4", headers=headers)


@router.get("/cameras/{camera_id}/export")
async def export_clip(
    request: Request,
    camera_id: str,
    start: datetime = Query(...),
    duration: float = Query(..., gt=0.0),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_roles("admin", "operator")),
):
    """Authorize and stream one bounded authoritative recording interval."""
    camera = await authorized_camera(session, camera_id, principal)
    return await stream_recording_clip(request, camera, start, duration, session)


def _bounded_segment_seconds(seconds: float) -> float:
    if not math.isfinite(seconds) or seconds < 0 or seconds > _MAX_SEGMENT_DURATION_SECONDS:
        raise ValueError("unsupported duration")
    return seconds


def _parse_duration(value: str) -> float:
    value = value.strip()
    if not value:
        raise ValueError("unsupported duration")
    try:
        numeric_value = float(value)
    except ValueError:
        numeric_value = None
    if numeric_value is not None:
        return _bounded_segment_seconds(numeric_value)

    m = _DURATION_RE.fullmatch(value)
    if not m or not any(m.group(name) for name in ("h", "m", "s")):
        raise ValueError("unsupported duration")
    return _bounded_segment_seconds(
        float(m.group("h") or 0) * 3600
        + float(m.group("m") or 0) * 60
        + float(m.group("s") or 0)
    )


def _segment_start(path: str, duration: float) -> datetime:
    # duration is not used. A malformed name must not be replaced with now.
    del duration
    # MediaMTX v1.21.1 requires %f in recordPath when playback is enabled.
    # Windows field-test segments use %s-%f: a 10-digit Unix-second prefix and
    # six zero-padded microseconds. Both separators are accepted so a Windows
    # path posted to this parser keeps those microseconds.
    normalized = str(path).replace("\\", "/")
    stem = Path(normalized.rsplit("/", 1)[-1]).stem
    match = _SEGMENT_NAME_RE.fullmatch(stem)
    if match is None:
        raise ValueError("malformed segment name")
    micros = int(match.group("micros") or "0")
    return datetime.fromtimestamp(int(match.group("seconds")), tz=timezone.utc).replace(
        microsecond=micros
    )


@internal_router.post("/segments/complete", status_code=202)
async def segment_complete(
    path: str = Form(...),
    segment_path: str = Form(...),
    duration: str = Form(...),
    recording_node_id: str | None = Form(default=None),
    assignment_generation: int | None = Form(default=None),
    x_recording_hook_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Accept a completed recording-segment hook and enqueue durable metadata.

    Args:
        path: Recording stream key reported by MediaMTX.
        segment_path: Completed segment path.
        duration: Segment duration string.
        recording_node_id: Optional distributed recorder identity.
        assignment_generation: Optional distributed fencing generation.
        x_recording_hook_token: Shared hook authentication token.
        session: Database session used for policy/fencing/outbox persistence.

    Returns:
        Acceptance object containing deterministic segment ID and dedupe result.

    Raises:
        HTTPException: If hook authentication, policy/camera lookup, duration,
            segment name, or distributed fencing evidence is invalid.
    """
    if not settings.recording_hook_token:
        raise HTTPException(503, "Recording hook token is not configured")
    if not x_recording_hook_token or not hmac.compare_digest(
        x_recording_hook_token, settings.recording_hook_token
    ):
        raise HTTPException(401, "Invalid recording hook token")

    policy = (
        await session.execute(
            select(RecordingPolicyEntity).where(RecordingPolicyEntity.record_stream_key == path)
        )
    ).scalar_one_or_none()
    if not policy:
        raise HTTPException(404, "Unknown recording path")
    camera = await session.get(CameraEntity, policy.camera_id)
    if not camera:
        raise HTTPException(404, "Camera not found")

    if settings.placement_execution_enabled and not recording_node_id:
        raise HTTPException(422, "Distributed recording hook must include recording_node_id")
    if settings.placement_execution_enabled and assignment_generation is None:
        raise HTTPException(422, "Distributed recording hook must include assignment_generation")

    actual_recording_node_id = recording_node_id or policy.recording_node_id

    try:
        seconds = _parse_duration(duration)
    except ValueError as exc:
        raise HTTPException(422, "Invalid segment duration") from exc

    try:
        start = _segment_start(segment_path, seconds)
    except ValueError as exc:
        raise HTTPException(422, "Malformed segment name") from exc
    completed = start + timedelta(seconds=seconds)

    if settings.placement_execution_enabled:
        await _validate_recording_evidence(
            session,
            camera_id=camera.id,
            recording_node_id=recording_node_id,
            assignment_generation=assignment_generation,
            evidence_time=completed,
        )
    segment_id = hashlib.sha256(
        f"{actual_recording_node_id}\0{assignment_generation}\0{path}\0{segment_path}".encode("utf-8")
    ).hexdigest()

    payload = {
        "segment_id": segment_id,
        "tenant_id": camera.tenant_id,
        "site_id": camera.site_id,
        "camera_id": camera.id,
        "recording_node_id": actual_recording_node_id,
        "assignment_generation": assignment_generation,
        "record_stream_key": policy.record_stream_key,
        "segment_path": segment_path,
        "segment_start": start.isoformat(),
        "duration_seconds": seconds,
        "completed_at": completed.isoformat(),
        "storage_tier": "hot",
        "object_uri": None,
    }
    inserted = False
    if settings.recording_metadata_events_enabled:
        inserted = await enqueue_message_once(
            session,
            message_id=f"recording:{segment_id}",
            topic=settings.kafka_topic_recordings,
            key_text=f"{camera.tenant_id}:{camera.id}",
            payload=payload,
        )
    await record_segment_completion(
        session,
        policy,
        segment_id=segment_id,
        completed_at=completed,
        recording_node_id=actual_recording_node_id,
        assignment_generation=assignment_generation,
    )
    await session.commit()
    return {
        "accepted": True,
        "segment_id": segment_id,
        "new": inserted,
        "metadata_event_enqueued": bool(settings.recording_metadata_events_enabled and inserted),
    }
