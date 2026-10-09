import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.entities import RecordingHealthStateEntity, RecordingPolicyEntity
from app.services import recording_health


FIXED_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


class _Session:
    def __init__(self, row=None):
        self.row = row
        self.added = []

    async def get(self, model, _key):
        assert model is RecordingHealthStateEntity
        return self.row

    def add(self, row):
        self.row = row
        self.added.append(row)


def _policy(*, enabled=True, segment_duration_seconds=900):
    return RecordingPolicyEntity(
        id="policy-1",
        camera_id="camera-1",
        mode="continuous" if enabled else "disabled",
        enabled=enabled,
        record_stream_key="camera-1-record",
        recording_node_id="record-a",
        retention_days=7,
        part_duration_ms=1000,
        segment_duration_seconds=segment_duration_seconds,
        max_part_size_mb=50,
    )


def test_gap_window_uses_segment_multiplier_and_configured_floor(monkeypatch):
    """Use the larger of the segment-derived interval and configured minimum."""
    monkeypatch.setattr(
        recording_health.settings,
        "observability_recording_gap_min_seconds",
        120,
    )
    monkeypatch.setattr(
        recording_health.settings,
        "observability_recording_gap_segment_multiplier",
        2.0,
    )

    assert recording_health.recording_gap_window_seconds(30) == 120
    assert recording_health.recording_gap_window_seconds(900) == 1800


def test_enabling_continuous_recording_initializes_startup_grace(monkeypatch):
    """Avoid flagging a gap before the first segment has had time to complete."""
    monkeypatch.setattr(
        recording_health.settings,
        "observability_recording_gap_min_seconds",
        120,
    )
    monkeypatch.setattr(
        recording_health.settings,
        "observability_recording_gap_segment_multiplier",
        2.0,
    )
    session = _Session()

    row = asyncio.run(
        recording_health.sync_recording_health_policy(
            session,
            _policy(),
            was_enabled=False,
            now=FIXED_NOW,
        )
    )

    assert row is session.row
    assert row.gap_deadline_at == FIXED_NOW + timedelta(seconds=1800)
    assert row.last_segment_completed_at is None


def test_segment_completion_advances_health_and_older_hook_cannot_regress(monkeypatch, tmp_path):
    """Advance from accepted segment evidence and ignore older delayed completions."""
    monkeypatch.setattr(
        recording_health.settings,
        "observability_recording_gap_min_seconds",
        120,
    )
    monkeypatch.setattr(
        recording_health.settings,
        "observability_recording_gap_segment_multiplier",
        2.0,
    )
    policy = _policy()
    completed = FIXED_NOW + timedelta(minutes=15)

    async def scenario():
        engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "health.db").as_posix())
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with factory() as session:
                first_row = await recording_health.record_segment_completion(
                    session,
                    policy,
                    segment_id="newer-segment",
                    completed_at=completed,
                    recording_node_id="record-b",
                    assignment_generation=7,
                    observed_at=completed,
                )
                older_row = await recording_health.record_segment_completion(
                    session,
                    policy,
                    segment_id="older-segment",
                    completed_at=completed - timedelta(minutes=5),
                    recording_node_id="record-a",
                    assignment_generation=6,
                    observed_at=completed + timedelta(seconds=1),
                )
                await session.commit()
            return first_row, older_row
        finally:
            await engine.dispose()

    first, older = asyncio.run(scenario())

    assert first.last_segment_id == "newer-segment"
    assert first.last_segment_completed_at == completed
    assert first.gap_deadline_at == completed + timedelta(seconds=1800)
    assert first.recording_node_id == "record-b"
    assert first.assignment_generation == 7
    assert older.last_segment_id == "newer-segment"
    assert older.last_segment_completed_at == completed
    assert older.recording_node_id == "record-b"
    assert older.assignment_generation == 7


def test_disabling_recording_clears_gap_deadline():
    """Do not alert on gap state for a recording policy that is disabled."""
    health = RecordingHealthStateEntity(
        camera_id="camera-1",
        last_segment_id="segment-1",
        last_segment_completed_at=FIXED_NOW,
        gap_deadline_at=FIXED_NOW + timedelta(minutes=30),
        recording_node_id="record-a",
        assignment_generation=1,
        observed_at=FIXED_NOW,
    )
    session = _Session(health)

    row = asyncio.run(
        recording_health.sync_recording_health_policy(
            session,
            _policy(enabled=False),
            was_enabled=True,
            now=FIXED_NOW,
        )
    )

    assert row.gap_deadline_at is None
    assert row.observed_at == FIXED_NOW
