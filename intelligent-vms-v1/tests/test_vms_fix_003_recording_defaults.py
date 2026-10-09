"""VMS-FIX-003: playback recordPath must include %f, and segment evidence stays strict."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, settings
from app.db.base import Base
from app.models.entities import RecordingPolicyEntity
from app.models.placement import PlacementAssignmentEntity, PlacementRevocationEntity
from app.routers import recordings
from app.services import recording_health
from app.services.mediamtx import MediaMTXClient, MediaMTXError


ROOT = Path(__file__).parents[1]
EPOCH = 1789999999
WINDOWS_SEGMENT = (
    "D:\\VMS\\Recordings\\cam\\2026\\10\\08\\18\\1789999999-123456.mp4"
)
POSIX_SEGMENT = "/recordings/cam/2026/10/08/18/1789999999-123456.mp4"
NEWER_SEGMENT = "/recordings/cam/2026/10/08/18/1789999999-000002.mp4"
OLDER_SEGMENT = "/recordings/cam/2026/10/08/18/1789999999-000001.mp4"
BOUNDARY_SEGMENT = "/recordings/cam/2026/10/08/18/1789999999-000000.mp4"
LEGACY_SEGMENT = "/recordings/cam/2026/10/08/18/1789999999.mp4"
OLD_TEMPLATE = "/recordings/%path/%Y/%m/%d/%H/%s"
PLAYBACK_TEMPLATE = "/recordings/%path/%Y/%m/%d/%H/%s-%f"
WINDOWS_TEMPLATE = "D:/VMS/Recordings/%path/%Y/%m/%d/%H/%s-%f"


def _completed(path: str, duration: str) -> datetime:
    start = recordings._segment_start(path, 0.0)
    return start + timedelta(seconds=recordings._parse_duration(duration))


def test_playback_defaults_and_examples_include_microseconds():
    """C3: defaults, the env example, and the recording design example include %f."""
    default = Settings.model_fields["recording_path_template"].default
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    design = (ROOT / "docs/architecture/PHASE3_RECORDING_DESIGN.md").read_text(encoding="utf-8")
    storage = (ROOT / "docs/operations/RECORDING_STORAGE.md").read_text(encoding="utf-8")
    windows = (ROOT / "deploy/windows/generate_windows_env.py").read_text(encoding="utf-8")

    assert default == PLAYBACK_TEMPLATE
    assert f"RECORDING_PATH_TEMPLATE={PLAYBACK_TEMPLATE}" in example
    assert f"recordPath: {PLAYBACK_TEMPLATE}" in design
    assert "C3" in storage
    assert "%s-%f" in windows


def test_settings_reject_record_path_that_playback_will_not_accept():
    """Refuse a template MediaMTX v1.21.1 rejects while playback is enabled."""
    with pytest.raises(ValidationError):
        Settings(recording_path_template=OLD_TEMPLATE)
    with pytest.raises(ValidationError):
        Settings(recording_path_template="/recordings/%Y-%m-%d_%H-%M-%S-%f")


def test_settings_accept_windows_and_default_playback_templates():
    """Accept the Windows %s-%f template and the control-plane default."""
    windows = Settings(recording_path_template=WINDOWS_TEMPLATE)
    default = Settings(recording_path_template=PLAYBACK_TEMPLATE)
    assert windows.recording_path_template == WINDOWS_TEMPLATE
    assert default.recording_path_template == PLAYBACK_TEMPLATE


def test_provisioning_rejects_record_path_without_microseconds(monkeypatch):
    """Do not send a playback recordPath that omits %f."""
    monkeypatch.setattr(settings, "recording_path_template", OLD_TEMPLATE)
    called = False

    async def fake_upsert(_stream_key, _payload):
        nonlocal called
        called = True

    client = MediaMTXClient("http://media.invalid")
    monkeypatch.setattr(client, "_upsert", fake_upsert)
    with pytest.raises(MediaMTXError, match="%f"):
        asyncio.run(
            client.add_or_replace_recording_path(
                "cam-record",
                "rtsp://10.0.0.2/main",
                retention_days=7,
                part_duration_ms=1000,
                segment_duration_seconds=900,
                max_part_size_mb=50,
            )
        )
    assert called is False


def test_windows_segment_names_keep_microseconds():
    """Parse existing Windows %s-%f names, including backslashes, without dropping %f."""
    expected = datetime.fromtimestamp(EPOCH, tz=timezone.utc).replace(microsecond=123456)
    assert recordings._segment_start(WINDOWS_SEGMENT, 900) == expected
    assert recordings._segment_start(POSIX_SEGMENT, 900) == expected
    one = recordings._segment_start(
        "/recordings/cam/2026/10/08/18/1789999999-000001.mp4",
        1,
    )
    assert one.microsecond == 1
    assert int(one.timestamp()) == EPOCH


def test_legacy_epoch_segment_name_has_zero_microseconds():
    """A 10-digit %s filename still parses, with no invented fractional time."""
    start = recordings._segment_start(LEGACY_SEGMENT, 1)
    assert start == datetime.fromtimestamp(EPOCH, tz=timezone.utc)
    assert start.microsecond == 0


@pytest.mark.parametrize(
    "path",
    [
        "not-a-segment.mp4",
        "/recordings/cam/1789999999-12.mp4",
        "/recordings/cam/1789999999-1234567.mp4",
        "/recordings/cam/1789999999-abcdef.mp4",
        "",
        "/recordings/cam/178999999.mp4",
        "D:/VMS/Recordings/cam/nope.mp4",
    ],
)
def test_malformed_segment_name_is_rejected(path):
    """Reject a segment filename that is not %s or Windows %s-%f."""
    with pytest.raises(ValueError, match="malformed segment name"):
        recordings._segment_start(path, 900)


def test_malformed_segment_name_does_not_invent_now(monkeypatch):
    """A bad segment name raises instead of substituting the current clock."""

    class NoInventedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("segment parser invented now")

    monkeypatch.setattr(recordings, "datetime", NoInventedNow)
    with pytest.raises(ValueError, match="malformed segment name"):
        recordings._segment_start("not-a-segment.mp4", 15)


def test_hook_duration_accepts_seconds_and_one_day_maximum():
    """MediaMTX reports seconds; the pinned runtime allows a segment of exactly one day."""
    assert recordings._parse_duration("900") == 900
    assert recordings._parse_duration("1.5") == 1.5
    assert recordings._parse_duration("15m0s") == 900
    assert recordings._parse_duration("1.5s") == 1.5
    assert recordings._parse_duration("0") == 0
    assert recordings._parse_duration("24h") == 24 * 60 * 60
    assert recordings._parse_duration("86400") == 24 * 60 * 60


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "inf",
        "-inf",
        "-1",
        "-0.1",
        "1e309",
        "25h",
        "24h0m1s",
        "86400.1",
        "1e20",
        "999999h",
        "",
    ],
)
def test_duration_rejects_non_finite_negative_and_unbounded(value):
    """Reject durations that are non-finite, negative, or above the one-day maximum."""
    with pytest.raises(ValueError, match="unsupported duration"):
        recordings._parse_duration(value)


def _policy():
    return RecordingPolicyEntity(
        id="policy-1",
        camera_id="camera-1",
        mode="continuous",
        enabled=True,
        record_stream_key="camera-1-record",
        recording_node_id="record-a",
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=900,
        max_part_size_mb=50,
    )


async def _two_completions(database: Path, policy, first: dict, second: dict):
    engine = create_async_engine("sqlite+aiosqlite:///" + database.as_posix())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            await recording_health.record_segment_completion(session, policy, **first)
            row = await recording_health.record_segment_completion(session, policy, **second)
            await session.commit()
            return row
    finally:
        await engine.dispose()


def test_health_does_not_regress_for_one_microsecond_older_completion(tmp_path):
    """One microsecond earlier is older evidence and must not move health backward."""
    newer_at = _completed(NEWER_SEGMENT, "60")
    older_at = _completed(OLDER_SEGMENT, "60")
    assert newer_at - older_at == timedelta(microseconds=1)
    policy = _policy()

    row = asyncio.run(
        _two_completions(
            tmp_path / "older.db",
            policy,
            {
                "segment_id": "newer-segment",
                "completed_at": newer_at,
                "recording_node_id": "record-b",
                "assignment_generation": 7,
                "observed_at": newer_at,
            },
            {
                "segment_id": "older-segment",
                "completed_at": older_at,
                "recording_node_id": "record-a",
                "assignment_generation": 6,
                "observed_at": newer_at,
            },
        )
    )

    assert row.last_segment_id == "newer-segment"
    assert row.last_segment_completed_at == newer_at


def test_health_equal_microsecond_completion_is_not_older(tmp_path):
    """An equal completion timestamp is the boundary that still advances health."""
    # 0 microseconds + 1s and 500000 microseconds + 0.5s are the same instant.
    # 0.5 is exact in binary floating point, so the comparison is not rounded away.
    first_at = _completed(BOUNDARY_SEGMENT, "1")
    second_at = _completed(
        "/recordings/cam/2026/10/08/18/1789999999-500000.mp4",
        "0.5",
    )
    assert first_at == second_at
    policy = _policy()

    row = asyncio.run(
        _two_completions(
            tmp_path / "equal.db",
            policy,
            {
                "segment_id": "boundary-newer",
                "completed_at": first_at,
                "recording_node_id": "record-b",
                "assignment_generation": 7,
                "observed_at": first_at,
            },
            {
                "segment_id": "boundary-equal",
                "completed_at": second_at,
                "recording_node_id": "record-a",
                "assignment_generation": 8,
                "observed_at": second_at,
            },
        )
    )

    assert row.last_segment_id == "boundary-equal"
    assert row.last_segment_completed_at == first_at


def _assignment(node_id, generation, lease_expires_at):
    return PlacementAssignmentEntity(
        id="pa-rec",
        camera_id="cam-1",
        role="recording",
        region_id="region-a",
        node_id=node_id,
        cleanup_node_ids_json=[],
        generation=generation,
        active=True,
        reason="initial",
        lease_expires_at=lease_expires_at,
        autonomy_expires_at=None,
        assigned_at=lease_expires_at,
    )


def test_fence_accepts_exact_authority_boundary_and_rejects_one_microsecond_later():
    """Active recording authority includes the exact completion and excludes the next microsecond."""
    boundary = _completed(BOUNDARY_SEGMENT, "60")
    late = _completed(OLDER_SEGMENT, "60")
    assert late - boundary == timedelta(microseconds=1)
    row = _assignment("node-a", 5, boundary)

    recordings._validate_recording_fence(row, "node-a", 5, evidence_time=boundary)
    with pytest.raises(HTTPException) as caught:
        recordings._validate_recording_fence(row, "node-a", 5, evidence_time=late)
    assert caught.value.status_code == 409


class _FenceSession:
    def __init__(self, assignment_row, revocation_row):
        self.assignment_row = assignment_row
        self.revocation_row = revocation_row
        self.calls = 0

    async def execute(self, _query):
        self.calls += 1
        if self.calls == 1:
            return _Scalar(self.assignment_row)
        return _Scalar(self.revocation_row)


class _Scalar:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


def test_revoked_fence_uses_microsecond_authority_boundary():
    """Revoked-generation evidence is inside the window at valid_until and outside one microsecond later."""
    boundary = _completed(BOUNDARY_SEGMENT, "60")
    late = _completed(OLDER_SEGMENT, "60")
    assert late - boundary == timedelta(microseconds=1)
    current = _assignment("node-b", 6, boundary + timedelta(hours=1))
    revoked = PlacementRevocationEntity(
        id="rev-rec",
        assignment_id=current.id,
        camera_id="cam-1",
        role="recording",
        node_id="node-a",
        revoked_generation=5,
        reason="failover",
        valid_until=boundary,
    )
    accepted = _FenceSession(current, revoked)
    rejected = _FenceSession(current, revoked)

    asyncio.run(
        recordings._validate_recording_evidence(
            accepted,
            camera_id="cam-1",
            recording_node_id="node-a",
            assignment_generation=5,
            evidence_time=boundary,
        )
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            recordings._validate_recording_evidence(
                rejected,
                camera_id="cam-1",
                recording_node_id="node-a",
                assignment_generation=5,
                evidence_time=late,
            )
        )
    assert caught.value.status_code == 409
