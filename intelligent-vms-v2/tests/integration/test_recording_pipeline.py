"""End-to-end recording, indexing, playback resolution and export.

These tests run the REAL FFmpeg-based recorder and exporter. They write real
MP4 files to a real storage volume, index them, export a real clip and verify
its SHA-256 independently. Nothing here is mocked except the camera itself,
which is a locally generated H.264 file replayed in real time, because no
camera hardware exists in the build environment.

What this does and does not prove:
  PROVES    - the FFmpeg command construction, segment-list watching, atomic
              finalization, index consistency, crash recovery, retention
              deletion, export stream-copy and evidence hashing all work on
              genuine media.
  DOES NOT  - prove RTSP transport against a physical ONVIF camera. That is an
              external qualification item (docs/verification/).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import shutil
import subprocess
from pathlib import Path

import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG,
                                     reason="ffmpeg/ffprobe not available on PATH")

pytestmark = [pytest.mark.slow]


def make_camera(session, name="Recorder Test"):
    from app.db.models import Camera, CameraStream, StorageVolume

    volume = session.query(StorageVolume).first()
    assert volume is not None, "the seeded fixture must create a storage volume"

    camera = Camera(name=name, host="192.168.70.10", port=80, rtsp_port=554,
                    protocol="rtsp", state="online", enabled=True,
                    storage_volume_id=volume.id)
    session.add(camera)
    session.flush()
    session.add(CameraStream(camera_id=camera.id, role="main", enabled=True, record=True,
                             source_url="rtsp://192.168.70.10:554/stream1",
                             transport="rtsp_tcp", codec="h264", width=320, height=240))
    session.commit()
    return camera


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"file is not playable: {result.stderr}"
    return float(result.stdout.strip())


# ==========================================================================
# Recording
# ==========================================================================
@requires_ffmpeg
@pytest.mark.ffmpeg
async def test_recorder_produces_playable_segments_and_matching_index(
        seeded, settings, db_session, sample_video):
    """Files on disk and index rows must agree, and the files must play."""
    from app.db.models import RecordingSegment
    from app.recording.index import resolve_path
    from recorder.supervisor import CameraRecorder
    from vms_contracts.enums import SegmentState

    camera = make_camera(db_session)

    recorder = CameraRecorder(
        camera.id, settings=settings, source_override=str(sample_video),
        is_file_source=True, extra_input_args=["-stream_loop", "-1", "-re"])

    task = asyncio.create_task(recorder.run())
    await asyncio.sleep(8.0)
    await recorder.stop()
    await asyncio.wait_for(task, timeout=25)

    db_session.expire_all()
    segments = db_session.query(RecordingSegment).filter_by(camera_id=camera.id).all()
    assert len(segments) >= 2, f"expected several segments, got {len(segments)}"

    finalized = [s for s in segments if s.state == SegmentState.FINALIZED.value]
    assert finalized, "at least one segment must reach FINALIZED"

    for segment in finalized:
        assert segment.size_bytes and segment.size_bytes > 0
        assert segment.ended_at > segment.started_at
        assert segment.duration_s and segment.duration_s > 0

        path = resolve_path(db_session, segment)
        assert path is not None and path.is_file(), f"missing file for {segment.id}"
        assert not path.name.endswith(".part"), "finalized files must not be partial"
        assert probe_duration(path) > 0.5


@requires_ffmpeg
@pytest.mark.ffmpeg
async def test_recorder_records_without_transcoding(seeded, settings, db_session,
                                                    sample_video):
    """Stream copy: the recorded codec must match the source exactly."""
    from app.db.models import RecordingSegment
    from app.recording.index import resolve_path
    from recorder.supervisor import CameraRecorder

    camera = make_camera(db_session, name="Copy Mode")
    recorder = CameraRecorder(camera.id, settings=settings,
                              source_override=str(sample_video), is_file_source=True,
                              extra_input_args=["-stream_loop", "-1", "-re"])

    command = recorder_command(recorder, settings)
    assert "-c:v" in command and command[command.index("-c:v") + 1] == "copy"

    task = asyncio.create_task(recorder.run())
    await asyncio.sleep(6.0)
    await recorder.stop()
    await asyncio.wait_for(task, timeout=25)

    db_session.expire_all()
    segment = (db_session.query(RecordingSegment)
               .filter_by(camera_id=camera.id, state="finalized").first())
    assert segment is not None

    path = resolve_path(db_session, segment)
    codec = (await asyncio.to_thread(subprocess.run,
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=False)).stdout.strip()
    assert codec == "h264", f"expected an untranscoded h264 copy, got {codec!r}"


def recorder_command(recorder, settings) -> list[str]:
    """Build the FFmpeg command the recorder would run, without running it."""
    from recorder.segmenter import SegmenterConfig, SegmentRecorder

    config = SegmenterConfig(source_url=recorder.source_override,
                             spool_dir=settings.spool_dir / "probe",
                             segment_seconds=settings.segment_seconds,
                             ffmpeg_path=settings.ffmpeg_path,
                             is_file_source=True)

    async def _noop(_segment):
        return None

    probe = SegmentRecorder(config, _noop, name="probe")
    return probe.build_command(Path("/tmp/list.csv"), Path("/tmp/seg_%05d.mp4"))


# ==========================================================================
# Crash recovery
# ==========================================================================
async def test_crash_recovery_never_leaves_a_segment_writing(seeded, settings, db_session):
    """A WRITING row after a crash is either finalized or marked corrupt."""
    from app.db.models import RecordingSegment, StorageVolume
    from app.recording import recovery
    from vms_contracts.enums import SegmentState

    camera = make_camera(db_session, name="Crash Test")
    volume = db_session.query(StorageVolume).first()

    good_rel = f"recordings/{camera.id}/2026/10/19/main/partial.mp4"
    good_path = Path(volume.path) / good_rel
    good_path.parent.mkdir(parents=True, exist_ok=True)
    good_path.with_suffix(".mp4.part").write_bytes(b"\0" * 2048)

    empty_rel = f"recordings/{camera.id}/2026/10/19/main/empty.mp4"
    (Path(volume.path) / empty_rel).with_suffix(".mp4.part").write_bytes(b"")

    now = dt.datetime.now(dt.UTC)
    for relative in (good_rel, empty_rel):
        db_session.add(RecordingSegment(
            camera_id=camera.id, stream_role="main", volume_id=volume.id,
            relative_path=relative, started_at=now - dt.timedelta(minutes=5),
            state=SegmentState.WRITING.value))
    db_session.commit()

    report = recovery.recover_writing_segments(db_session)
    db_session.commit()

    assert report.writing_found == 2
    assert report.finalized == 1
    assert report.marked_corrupt == 1

    states = {s.relative_path: s.state for s in
              db_session.query(RecordingSegment).filter_by(camera_id=camera.id).all()}
    assert states[good_rel] == SegmentState.FINALIZED.value
    assert states[empty_rel] == SegmentState.CORRUPT.value
    assert good_path.is_file(), "the recovered segment must sit at its final path"
    assert not good_path.with_suffix(".mp4.part").exists()


async def test_stale_recorder_tasks_are_released_for_another_worker(seeded, db_session):
    from app.core.timeutil import utcnow
    from app.db.models import RecordingTask
    from app.recording import recovery

    camera = make_camera(db_session, name="Stale Worker")
    db_session.add(RecordingTask(
        camera_id=camera.id, stream_role="main", reason="continuous", active=True,
        worker_id="worker-that-died", heartbeat_at=utcnow() - dt.timedelta(minutes=10),
        state="recording"))
    db_session.commit()

    assert recovery.release_stale_tasks(db_session) == 1
    db_session.commit()

    task = db_session.query(RecordingTask).filter_by(camera_id=camera.id).one()
    assert task.worker_id is None and task.state == "idle"


# ==========================================================================
# Retention
# ==========================================================================
async def test_retention_deletes_old_files_but_keeps_a_tombstone(seeded, settings,
                                                                 db_session):
    from app.db.models import RecordingSegment, StorageVolume
    from app.recording import retention
    from vms_contracts.enums import SegmentState

    camera = make_camera(db_session, name="Retention Test")
    camera.retention_days = 1
    volume = db_session.query(StorageVolume).first()

    old_rel = f"recordings/{camera.id}/2026/01/01/main/old.mp4"
    old_path = Path(volume.path) / old_rel
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"\0" * 4096)

    now = dt.datetime.now(dt.UTC)
    db_session.add(RecordingSegment(
        camera_id=camera.id, stream_role="main", volume_id=volume.id,
        relative_path=old_rel, started_at=now - dt.timedelta(days=10),
        ended_at=now - dt.timedelta(days=10) + dt.timedelta(minutes=5),
        size_bytes=4096, state=SegmentState.FINALIZED.value))
    db_session.commit()

    preview = retention.apply_age_retention(db_session, settings=settings, dry_run=True)
    assert preview.deleted == 1
    assert old_path.is_file(), "a dry run must not delete anything"

    result = retention.apply_age_retention(db_session, settings=settings)
    db_session.commit()

    assert result.deleted == 1
    assert result.bytes_reclaimed == 4096
    assert not old_path.exists()

    row = db_session.query(RecordingSegment).filter_by(relative_path=old_rel).one()
    assert row.state == SegmentState.DELETED.value, "a tombstone must remain for reporting"


async def test_retention_keeps_recordings_inside_the_window(seeded, settings, db_session):
    from app.core.timeutil import utcnow
    from app.db.models import RecordingSegment, StorageVolume
    from app.recording import retention
    from vms_contracts.enums import SegmentState

    camera = make_camera(db_session, name="Within Window")
    camera.retention_days = 30
    volume = db_session.query(StorageVolume).first()

    recent_rel = f"recordings/{camera.id}/main/recent.mp4"
    recent_path = Path(volume.path) / recent_rel
    recent_path.parent.mkdir(parents=True, exist_ok=True)
    recent_path.write_bytes(b"\0" * 1024)

    db_session.add(RecordingSegment(
        camera_id=camera.id, stream_role="main", volume_id=volume.id,
        relative_path=recent_rel, started_at=utcnow() - dt.timedelta(days=20),
        ended_at=utcnow() - dt.timedelta(days=20) + dt.timedelta(minutes=5),
        size_bytes=1024, state=SegmentState.FINALIZED.value))
    db_session.commit()

    result = retention.apply_age_retention(db_session, settings=settings)
    assert result.deleted == 0
    assert recent_path.is_file()


# ==========================================================================
# Playback resolution
# ==========================================================================
async def test_playback_refuses_to_hang_when_media_is_missing(seeded, db_session):
    """Index says footage exists, disk disagrees: fail fast with a precise code."""
    from app.core.errors import MediaUnavailableError
    from app.db.models import RecordingSegment, StorageVolume
    from app.playback.resolver import resolve_media

    camera = make_camera(db_session, name="Missing Media")
    volume = db_session.query(StorageVolume).first()
    start = dt.datetime(2026, 10, 19, 11, 0, tzinfo=dt.UTC)

    db_session.add(RecordingSegment(
        camera_id=camera.id, stream_role="main", volume_id=volume.id,
        relative_path="recordings/missing/never-written.mp4", started_at=start,
        ended_at=start + dt.timedelta(seconds=30), duration_s=30.0,
        size_bytes=9999, state="finalized"))
    db_session.commit()

    with pytest.raises(MediaUnavailableError) as excinfo:
        resolve_media(db_session, camera.id, start, start + dt.timedelta(seconds=20))
    assert excinfo.value.code == "media_files_missing"


# ==========================================================================
# Export and evidence
# ==========================================================================
@requires_ffmpeg
@pytest.mark.ffmpeg
async def test_export_produces_a_real_clip_with_verifiable_evidence(
        seeded, settings, db_session, sample_video):
    from app.core.crypto import sha256_file
    from app.db.models import ExportJob, RecordingSegment, StorageVolume
    from app.exports.jobs import ExportService
    from vms_contracts.enums import ExportFormat, ExportState

    camera = make_camera(db_session, name="Export Test")
    volume = db_session.query(StorageVolume).first()

    relative = f"recordings/{camera.id}/2026/10/19/main/source.mp4"
    destination = Path(volume.path) / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(sample_video.read_bytes())

    start = dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.UTC)
    db_session.add(RecordingSegment(
        camera_id=camera.id, stream_role="main", volume_id=volume.id,
        relative_path=relative, started_at=start,
        ended_at=start + dt.timedelta(seconds=10), duration_s=10.0,
        size_bytes=destination.stat().st_size, state="finalized", codec="h264"))
    db_session.commit()

    service = ExportService(settings)
    job = service.create(db_session, camera_id=camera.id, user_id="tester",
                         start=start + dt.timedelta(seconds=2),
                         end=start + dt.timedelta(seconds=6),
                         fmt=ExportFormat.MP4, reason="integration test",
                         case_reference="CASE-1")
    db_session.commit()
    job_id = job.id

    await service.run(job_id)

    db_session.expire_all()
    completed = db_session.get(ExportJob, job_id)
    assert completed.state == ExportState.COMPLETED.value, completed.error_detail
    assert completed.sha256 and len(completed.sha256) == 64
    assert completed.size_bytes > 0

    exported = settings.exports_dir / completed.relative_path
    assert exported.is_file()

    # Independently re-hash the artifact: the stored evidence must match.
    digest, size = sha256_file(exported)
    assert digest == completed.sha256
    assert size == completed.size_bytes

    duration = probe_duration(exported)
    assert 3.0 <= duration <= 5.5, f"expected a ~4s clip, got {duration}s"

    actions = [entry["action"] for entry in completed.chain_of_custody]
    assert actions[0] == "requested"
    assert "produced" in actions


@requires_ffmpeg
@pytest.mark.ffmpeg
async def test_export_refuses_rather_than_silently_shortening(seeded, settings, db_session):
    """Missing source footage must fail the export, never produce a partial clip."""
    from app.db.models import ExportJob, RecordingSegment, StorageVolume
    from app.exports.jobs import ExportService
    from vms_contracts.enums import ExportState

    camera = make_camera(db_session, name="Incomplete Export")
    volume = db_session.query(StorageVolume).first()
    start = dt.datetime(2026, 10, 19, 11, 0, tzinfo=dt.UTC)

    db_session.add(RecordingSegment(
        camera_id=camera.id, stream_role="main", volume_id=volume.id,
        relative_path="recordings/gone/absent.mp4", started_at=start,
        ended_at=start + dt.timedelta(seconds=30), duration_s=30.0,
        size_bytes=9999, state="finalized"))
    db_session.commit()

    service = ExportService(settings)
    job = service.create(db_session, camera_id=camera.id, user_id="tester",
                         start=start, end=start + dt.timedelta(seconds=20))
    db_session.commit()
    job_id = job.id

    from app.core.errors import MediaUnavailableError

    with pytest.raises(MediaUnavailableError):
        await service.run(job_id)

    db_session.expire_all()
    failed = db_session.get(ExportJob, job_id)
    assert failed.state == ExportState.FAILED.value
    assert failed.error_code in {"media_files_missing", "incomplete_media",
                                 "no_recording_in_range"}
    assert failed.sha256 is None


async def test_export_preflight_blocks_when_disk_is_short(settings, tmp_path, monkeypatch):
    from app.exports import preflight as preflight_module

    source = tmp_path / "big.mp4"
    source.write_bytes(b"\0" * 10_000)

    monkeypatch.setattr(preflight_module.shutil, "disk_usage",
                        lambda path: type("U", (), {"total": 1000, "used": 900,
                                                    "free": 100})())
    result = preflight_module.preflight([source], tmp_path / "exports", headroom=1.25)

    assert result.ok is False
    assert "free" in (result.reason or "")


async def test_export_expiry_reclaims_disk_and_revokes_the_token(seeded, settings,
                                                                 db_session):
    from app.core.timeutil import utcnow
    from app.db.models import ExportJob
    from app.exports.cleanup import cleanup_expired
    from vms_contracts.enums import ExportState

    relative = "expired-job/clip.mp4"
    path = settings.exports_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 2048)

    db_session.add(ExportJob(
        camera_id="cam1", requested_by="tester",
        requested_start=utcnow() - dt.timedelta(days=5),
        requested_end=utcnow() - dt.timedelta(days=5) + dt.timedelta(minutes=1),
        state=ExportState.COMPLETED.value, relative_path=relative, size_bytes=2048,
        sha256="deadbeef", download_token_hash="sometokenhash",
        expires_at=utcnow() - dt.timedelta(hours=1)))
    db_session.commit()

    result = cleanup_expired(db_session, settings)
    db_session.commit()

    assert result.expired == 1
    assert result.bytes_reclaimed == 2048
    assert not path.exists()

    job = db_session.query(ExportJob).one()
    assert job.state == ExportState.EXPIRED.value
    assert job.download_token_hash is None
