"""Unit tests for the domain services: scheduling, gaps, storage, PTZ, events,
analytics, licensing, capacity and RBAC."""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.timeutil import utcnow


# -- recording schedule ----------------------------------------------------
def test_schedule_is_evaluated_in_site_local_time():
    from app.recording.planner import parse_blocks, should_record

    blocks = parse_blocks([{"day": 0, "start": "08:00", "end": "18:00"}])
    monday_0500_utc = dt.datetime(2026, 10, 19, 5, 0, tzinfo=dt.timezone.utc)
    # 05:00 UTC is 10:30 in Kolkata (inside the window) and 05:00 UTC (outside).
    assert should_record(blocks, "Asia/Kolkata", monday_0500_utc)[0] is True
    assert should_record(blocks, "UTC", monday_0500_utc)[0] is False


def test_empty_schedule_records_continuously():
    from app.recording.planner import should_record

    active, mode = should_record([], "UTC")
    assert active and mode.value == "continuous"


def test_schedule_rejects_inverted_block():
    from app.recording.planner import parse_blocks

    with pytest.raises(ValueError):
        parse_blocks([{"day": 1, "start": "18:00", "end": "08:00"}])


# -- gaps ------------------------------------------------------------------
def test_gap_detection_finds_holes(seeded, db_session):
    from app.db.models import RecordingSegment
    from app.recording.gaps import continuity_percent, find_gaps

    start = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)
    for offset, length in ((0, 300), (600, 300)):      # 5-minute hole between
        db_session.add(RecordingSegment(
            camera_id="cam1", stream_role="main", relative_path=f"r/{offset}.mp4",
            started_at=start + dt.timedelta(seconds=offset),
            ended_at=start + dt.timedelta(seconds=offset + length),
            duration_s=length, size_bytes=1000, state="finalized"))
    db_session.commit()

    gaps = find_gaps(db_session, "cam1", start, start + dt.timedelta(seconds=900))
    assert len(gaps) == 1
    assert gaps[0].duration_s == pytest.approx(300, abs=1)
    assert continuity_percent(db_session, "cam1", start,
                              start + dt.timedelta(seconds=900)) == pytest.approx(66.67,
                                                                                  abs=0.1)


def test_timeline_buckets_reflect_recorded_time(seeded, db_session):
    from app.db.models import RecordingSegment
    from app.recording.index import timeline_buckets

    start = dt.datetime(2026, 10, 19, 0, 0, tzinfo=dt.timezone.utc)
    db_session.add(RecordingSegment(
        camera_id="cam2", stream_role="main", relative_path="r/a.mp4", started_at=start,
        ended_at=start + dt.timedelta(hours=1), duration_s=3600, size_bytes=1,
        state="finalized"))
    db_session.commit()

    buckets = timeline_buckets(db_session, "cam2", start, start + dt.timedelta(hours=24),
                               bucket_count=24)
    assert len(buckets) == 24
    assert buckets[0]["recorded_s"] == pytest.approx(3600, abs=1)
    assert buckets[1]["has_gap"] is True


# -- storage ---------------------------------------------------------------
def test_volume_state_transitions_on_low_space(seeded, db_session, settings, tmp_path,
                                               monkeypatch):
    from app.storage import volumes as volume_service

    volume = volume_service.create_volume(db_session, name="test-vol",
                                          path=str(tmp_path / "vol"),
                                          min_free_percent=20.0)
    db_session.commit()

    monkeypatch.setattr(volume_service, "measure",
                        lambda path: volume_service.VolumeUsage(total_bytes=1000,
                                                                used_bytes=970,
                                                                free_bytes=30))
    state, events = volume_service.refresh_volume(db_session, volume, settings)
    assert state.value == "full"
    assert events and events[0]["type"].value == "storage.critical"


def test_writable_volumes_excludes_offline(seeded, db_session, tmp_path):
    from app.storage import volumes as volume_service

    volume = volume_service.create_volume(db_session, name="offline-vol",
                                          path=str(tmp_path / "v2"))
    volume.state = "offline"
    db_session.commit()
    assert "offline-vol" not in {v.name
                                 for v in volume_service.writable_volumes(db_session)}


def test_allocator_builds_confined_dated_paths(seeded, db_session):
    from app.db.models import Camera, StorageVolume
    from app.storage import allocator

    volume = db_session.query(StorageVolume).first()
    camera = Camera(name="A", host="192.168.1.1", storage_volume_id=volume.id)
    db_session.add(camera)
    db_session.commit()

    allocation = allocator.allocate(
        db_session, camera, role="main",
        start=dt.datetime(2026, 3, 7, 14, 30, tzinfo=dt.timezone.utc), segment_id="abc123")
    assert "/2026/03/07/main/" in allocation.relative_path
    assert allocation.absolute_path.is_relative_to(volume.path)
    assert allocation.temp_path.name.endswith(".part")


def test_allocator_fails_loudly_without_volume(seeded, db_session):
    from app.db.models import Camera, StorageVolume
    from app.storage import allocator

    for volume in db_session.query(StorageVolume).all():
        volume.enabled = False
    camera = Camera(name="B", host="192.168.1.2")
    db_session.add(camera)
    db_session.commit()

    with pytest.raises(RuntimeError, match="no writable storage"):
        allocator.allocate(db_session, camera, role="main", start=utcnow(),
                           segment_id="deadbeef")


def test_raid_mdstat_parsing_detects_degraded():
    from app.storage.raid import parse_mdstat

    arrays = parse_mdstat("""Personalities : [raid1]
md0 : active raid1 sdb1[1] sda1[0]
      976630464 blocks super 1.2 [2/1] [U_]
unused devices: <none>""")
    assert len(arrays) == 1
    assert arrays[0].device == "md0"
    assert arrays[0].state == "degraded"
    assert arrays[0].healthy is False


# -- stream policy ---------------------------------------------------------
def test_stream_policy_picks_sub_for_small_tiles():
    from vms_contracts.enums import StreamRole
    from app.media.stream_policy import StreamOption, select_stream

    options = [StreamOption(StreamRole.MAIN, 1920, 1080, 4096),
               StreamOption(StreamRole.SUB, 640, 360, 512)]
    assert select_stream(options, tile_width=320, tile_height=180).role is StreamRole.SUB
    assert select_stream(options, tile_width=1600, tile_height=900).role is StreamRole.MAIN


def test_stream_policy_respects_bandwidth_budget():
    from vms_contracts.enums import StreamRole
    from app.media.stream_policy import StreamOption, select_stream

    options = [StreamOption(StreamRole.MAIN, 1920, 1080, 8192),
               StreamOption(StreamRole.SUB, 640, 360, 512)]
    result = select_stream(options, tile_width=1920, tile_height=1080,
                           remaining_budget_kbps=1000)
    assert result.role is StreamRole.SUB
    assert "bandwidth" in result.reason


def test_stream_policy_override_wins():
    from vms_contracts.enums import StreamRole
    from app.media.stream_policy import StreamOption, select_stream

    options = [StreamOption(StreamRole.MAIN, 1920, 1080, 4096),
               StreamOption(StreamRole.SUB, 640, 360, 512)]
    assert select_stream(options, tile_width=160, tile_height=90,
                         override=StreamRole.MAIN).role is StreamRole.MAIN


# -- PTZ arbitration -------------------------------------------------------
@pytest.fixture
def ptz_cameras(seeded, db_session):
    """PTZ locks reference real cameras (enforced by a foreign key)."""
    from app.db.models import Camera

    for index, camera_id in enumerate(("cam1", "cam-moving", "cam-dead")):
        camera = Camera(name=camera_id, host=f"192.168.44.{index + 1}")
        camera.id = camera_id
        db_session.add(camera)
    db_session.commit()
    return ["cam1", "cam-moving", "cam-dead"]


def test_ptz_lock_blocks_second_operator(ptz_cameras, db_session, settings):
    from app.core.errors import ConflictError
    from app.ptz.arbiter import PtzArbiter

    arbiter = PtzArbiter(settings)
    arbiter.acquire(db_session, "cam1", "user-a", owner_name="Alice")
    with pytest.raises(ConflictError) as excinfo:
        arbiter.acquire(db_session, "cam1", "user-b", owner_name="Bob")
    assert excinfo.value.code == "ptz_locked"


def test_ptz_higher_priority_preempts(ptz_cameras, db_session, settings):
    from app.ptz.arbiter import PtzArbiter

    arbiter = PtzArbiter(settings)
    arbiter.acquire(db_session, "cam1", "user-a", priority=0)
    grant = arbiter.acquire(db_session, "cam1", "supervisor", priority=10)
    assert grant.owner_id == "supervisor" and grant.preempted is not None


def test_ptz_move_duration_is_clamped(ptz_cameras, db_session, settings):
    from app.ptz.arbiter import PtzArbiter

    settings.ptz_max_move_seconds = 5.0
    arbiter = PtzArbiter(settings)
    arbiter.acquire(db_session, "cam1", "user-a")
    moving_until = arbiter.mark_moving(db_session, "cam1", "user-a", duration_s=600)
    assert (moving_until - utcnow()).total_seconds() <= 5.5


def test_ptz_watchdog_identifies_runaway_and_dead_clients(ptz_cameras, db_session, settings):
    from app.db.models import PtzLock
    from app.ptz.arbiter import PtzArbiter

    arbiter = PtzArbiter(settings)
    arbiter.acquire(db_session, "cam-moving", "user-a")
    arbiter.mark_moving(db_session, "cam-moving", "user-a", duration_s=1)
    db_session.get(PtzLock, "cam-moving").moving_until = utcnow() - dt.timedelta(seconds=1)

    arbiter.acquire(db_session, "cam-dead", "user-b")
    db_session.get(PtzLock, "cam-dead").expires_at = utcnow() - dt.timedelta(seconds=30)
    db_session.commit()

    assert set(arbiter.cameras_needing_stop(db_session)) == {"cam-moving", "cam-dead"}


def test_ptz_startup_clears_all_locks(ptz_cameras, db_session, settings):
    from app.ptz.arbiter import PtzArbiter

    arbiter = PtzArbiter(settings)
    arbiter.acquire(db_session, "cam1", "user-a")
    db_session.commit()
    assert arbiter.stop_all_on_startup(db_session) == ["cam1"]


# -- events ----------------------------------------------------------------
def test_event_dedupe_collapses_repeats():
    from app.events_engine.dedupe import DedupeWindow, dedupe_key

    clock = {"t": 0.0}
    window = DedupeWindow(window_s=10, clock=lambda: clock["t"])
    key = dedupe_key("camera.motion", "cam1", "zone-a")

    assert window.check(key).suppressed is False
    assert window.check(key).suppressed is True
    clock["t"] = 11
    assert window.check(key).suppressed is False


def test_rule_conditions_language():
    from app.events_engine.ingest import conditions_match

    payload = {"object_class": "person", "confidence": 0.82}
    assert conditions_match({"field": "object_class", "op": "eq", "value": "person"}, payload)
    assert conditions_match({"all": [{"field": "object_class", "op": "eq",
                                      "value": "person"},
                                     {"field": "confidence", "op": "gte", "value": 0.8}]},
                            payload)
    assert not conditions_match({"field": "confidence", "op": "gte", "value": 0.9}, payload)


def test_rule_validation_rejects_unknown_operator():
    from app.core.errors import ValidationError
    from app.events_engine.rules import validate_conditions

    with pytest.raises(ValidationError):
        validate_conditions({"field": "x", "op": "regex", "value": ".*"})


def test_rule_schedule_window_crossing_midnight():
    from app.events_engine.ingest import schedule_active

    schedule = {"start": "22:00", "end": "06:00"}
    assert schedule_active(schedule, dt.datetime(2026, 1, 1, 23, 0, tzinfo=dt.timezone.utc))
    assert schedule_active(schedule, dt.datetime(2026, 1, 1, 2, 0, tzinfo=dt.timezone.utc))
    assert not schedule_active(schedule,
                               dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc))


def test_alarm_state_machine_transitions():
    from vms_contracts.enums import AlarmState
    from app.core.errors import ConflictError
    from app.events_engine.alarms import assert_transition

    assert_transition(AlarmState.NEW, AlarmState.ACKNOWLEDGED)
    with pytest.raises(ConflictError):
        assert_transition(AlarmState.CLOSED, AlarmState.ACTIVE)


def test_alarm_aggregation_by_dedupe_key(seeded, db_session):
    from vms_contracts.enums import EventSeverity
    from app.events_engine.alarms import raise_alarm

    first = raise_alarm(db_session, title="Offline", severity=EventSeverity.HIGH,
                        camera_id="cam1", dedupe_key="camera.offline|cam1")
    second = raise_alarm(db_session, title="Offline", severity=EventSeverity.HIGH,
                         camera_id="cam1", dedupe_key="camera.offline|cam1")
    assert first.id == second.id
    assert second.event_count == 2
    assert second.state == "active"


# -- analytics -------------------------------------------------------------
def test_tripwire_fires_once_per_track():
    from app.analytics.zones import ZoneEvaluator

    evaluator = ZoneEvaluator()
    line = [(0.5, 0.0), (0.5, 1.0)]
    assert evaluator.evaluate(zone_id="z", kind="tripwire", polygon=line,
                              bbox=[0.2, 0.4, 0.1, 0.1], track_id="t1") == []
    hits = evaluator.evaluate(zone_id="z", kind="tripwire", polygon=line,
                              bbox=[0.7, 0.4, 0.1, 0.1], track_id="t1")
    assert len(hits) == 1 and hits[0].rule == "tripwire"
    assert evaluator.evaluate(zone_id="z", kind="tripwire", polygon=line,
                              bbox=[0.9, 0.4, 0.1, 0.1], track_id="t1") == []


def test_loitering_requires_dwell_time():
    from app.analytics.zones import ZoneEvaluator

    clock = {"t": 0.0}
    evaluator = ZoneEvaluator(clock=lambda: clock["t"])
    square = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    assert evaluator.evaluate(zone_id="z", kind="region", polygon=square,
                              bbox=[0.4, 0.4, 0.1, 0.1], track_id="t1",
                              dwell_seconds=5) == []
    clock["t"] = 6.0
    hits = evaluator.evaluate(zone_id="z", kind="region", polygon=square,
                              bbox=[0.4, 0.4, 0.1, 0.1], track_id="t1", dwell_seconds=5)
    assert hits and hits[0].rule == "loitering"


def test_plate_normalization_and_fuzzy_watchlist():
    from app.analytics.anpr import WatchlistEntry, match_watchlist, normalize_plate

    assert normalize_plate("dl 1c-ab 1234") == "DL1CAB1234"
    watchlist = [WatchlistEntry(plate="DL1CAB1234", label="stolen")]
    assert match_watchlist("DL1CAB1234", watchlist) is not None
    assert match_watchlist("DL1CA81234", watchlist) is not None      # 8/B confusion
    assert match_watchlist("XX9999", watchlist) is None


def test_privacy_requires_feature_enabled(settings):
    from app.analytics import privacy
    from app.core.errors import PermissionDeniedError

    settings.face_recognition_enabled = False
    with pytest.raises(PermissionDeniedError):
        privacy.assert_enabled(privacy.CAPABILITY_FACE, settings)


def test_privacy_retention_strips_biometrics(seeded, db_session, settings):
    from app.analytics import privacy
    from app.db.models import AnalyticsEvent

    settings.privacy_retention_days = 1
    db_session.add(AnalyticsEvent(
        camera_id="cam1", source="camera_edge", object_class="license_plate",
        occurred_at=utcnow() - dt.timedelta(days=5), plate_text="DL1CAB1234",
        attributes={"plate_image": "base64...", "colour": "white"}))
    db_session.commit()

    assert privacy.apply_privacy_retention(db_session, settings) == 1
    row = db_session.query(AnalyticsEvent).one()
    assert row.plate_text is None
    assert "plate_image" not in row.attributes
    assert row.attributes["colour"] == "white", "non-biometric attributes are retained"


def test_onvif_motion_maps_to_camera_edge_source():
    from vms_contracts.enums import AnalyticsSource
    from app.analytics.ingest import from_onvif_notification

    result = from_onvif_notification("cam1", {
        "topic": "tns1:RuleEngine/CellMotionDetector/Motion",
        "data": {"IsMotion": "true"}, "utc_time": "2026-10-19T12:00:00Z"})
    assert result is not None
    assert result.source is AnalyticsSource.CAMERA_EDGE, \
        "camera-produced analytics must never be labelled server inference"


# -- licensing / capacity --------------------------------------------------
def test_unlicensed_mode_is_limited_but_functional(settings):
    from app.licensing.entitlements import unlicensed

    entitlements = unlicensed(settings)
    assert entitlements.valid is False
    assert entitlements.max_cameras == settings.unlicensed_camera_limit


def test_licence_quota_enforced_on_new_cameras(seeded, db_session, settings):
    from app.core.errors import LicenseError
    from app.db.models import Camera
    from app.licensing.entitlements import unlicensed

    entitlements = unlicensed(settings)
    for index in range(entitlements.max_cameras):
        db_session.add(Camera(name=f"c{index}", host=f"192.168.2.{index + 1}"))
    db_session.commit()
    with pytest.raises(LicenseError):
        entitlements.check_camera_quota(db_session)


def test_signed_licence_round_trip_and_tamper_detection():
    from vms_contracts.enums import Edition
    from app.core.crypto import generate_signing_keypair
    from app.licensing import license_file

    private, public = generate_signing_keypair()
    document = license_file.issue(private, customer="Acme", edition=Edition.PRO,
                                  cameras=64, features=["multi_site"])
    result = license_file.parse_and_verify(document, public)
    assert result.valid and result.license.cameras == 64

    tampered = license_file.parse_and_verify(document.replace('"cameras": 64',
                                                              '"cameras": 640'), public)
    assert not tampered.valid
    assert "signature" in tampered.reason


def test_capacity_model_scales_with_cameras():
    from app.capacity.model import CameraClass, Workload, estimate

    small = estimate(Workload(classes=[CameraClass(count=16)], retention_days=30))
    large = estimate(Workload(classes=[CameraClass(count=256)], retention_days=30))
    assert large.total_ingest_mbps == pytest.approx(small.total_ingest_mbps * 16, rel=0.01)
    assert large.cpu_cores_recommended > small.cpu_cores_recommended
    assert "not a measured benchmark" in large.assumptions["model"]


def test_h265_estimates_less_storage_than_h264():
    from app.capacity.model import CameraClass, Workload, estimate

    h264 = estimate(Workload(classes=[CameraClass(count=10, codec="h264")]))
    h265 = estimate(Workload(classes=[CameraClass(count=10, codec="h265")]))
    assert h265.storage_bytes_per_day < h264.storage_bytes_per_day


# -- RBAC ------------------------------------------------------------------
def test_principal_scope_fails_closed():
    from app.core.rbac import Principal

    principal = Principal(user_id="u", username="viewer", roles=("viewer",),
                          permissions=frozenset({"live.view"}))
    assert principal.may_access_camera("cam1", "site1") is False


def test_camera_deny_beats_grant():
    from app.core.rbac import Principal

    principal = Principal(user_id="u", username="op", roles=("operator",),
                          permissions=frozenset(), camera_grants=frozenset({"cam1"}),
                          camera_denies=frozenset({"cam1"}), site_ids=frozenset({"s1"}))
    assert principal.may_access_camera("cam1", "s1") is False


def test_admin_without_scope_sees_everything():
    from app.core.rbac import Principal

    admin = Principal(user_id="a", username="admin", roles=("administrator",),
                      permissions=frozenset(), is_admin=True)
    assert admin.may_access_camera("any", "any") is True


def test_builtin_role_permission_sets_are_distinct():
    from vms_contracts.enums import Permission
    from app.core.rbac import ADMIN_PERMISSIONS, OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS

    assert Permission.PTZ_CONTROL in OPERATOR_PERMISSIONS
    assert Permission.PTZ_CONTROL not in VIEWER_PERMISSIONS
    assert Permission.USER_MANAGE not in OPERATOR_PERMISSIONS
    assert Permission.USER_MANAGE in ADMIN_PERMISSIONS


# -- devices ---------------------------------------------------------------
def test_duplicate_detection_by_serial_and_host(seeded, db_session):
    from app.db.models import Camera
    from app.devices.dedupe import find_duplicate

    db_session.add(Camera(name="Existing", host="192.168.5.5", port=80,
                          serial="SN-12345", mac="AA:BB:CC:DD:EE:FF"))
    db_session.commit()

    assert find_duplicate(db_session, serial="SN-12345").confidence == "certain"
    assert find_duplicate(db_session, mac="aabbccddeeff").confidence == "certain"
    assert find_duplicate(db_session, host="192.168.5.5", port=80).confidence == "likely"
    assert find_duplicate(db_session, host="192.168.5.6") is None


def test_capabilities_default_to_unproven():
    from app.devices.base import Capabilities

    caps = Capabilities()
    assert caps.any_ptz is False and caps.profile_s is False
    assert Capabilities.from_dict(caps.to_dict()).any_ptz is False


def test_profiles_are_classified_by_resolution():
    from vms_contracts.enums import StreamRole
    from app.devices.base import StreamProfile, classify_profiles

    profiles = [StreamProfile(role=StreamRole.MAIN, name="Profile_2", width=640, height=360),
                StreamProfile(role=StreamRole.MAIN, name="Profile_1", width=1920,
                              height=1080)]
    classified = classify_profiles(profiles)
    assert classified[StreamRole.MAIN].name == "Profile_1"
    assert classified[StreamRole.SUB].name == "Profile_2"


def test_onvif_notification_parsing():
    import xml.etree.ElementTree as ET

    from app.devices.onvif_adapter import parse_notification_messages

    xml = """<s:Body xmlns:s="http://www.w3.org/2003/05/soap-envelope"
        xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
        xmlns:tt="http://www.onvif.org/ver10/schema">
      <wsnt:NotificationMessage>
        <wsnt:Topic>tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="2026-10-19T12:00:00Z" PropertyOperation="Changed">
            <tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="VSC0"/></tt:Source>
            <tt:Data><tt:SimpleItem Name="IsMotion" Value="true"/></tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
    </s:Body>"""
    messages = parse_notification_messages(ET.fromstring(xml))
    assert len(messages) == 1
    assert "CellMotionDetector" in messages[0]["topic"]
    assert messages[0]["data"]["IsMotion"] == "true"


def test_ws_discovery_probe_match_parsing():
    from app.devices.discovery import parse_probe_match

    payload = """<?xml version="1.0"?>
    <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
        xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
        xmlns:wsa="http://www.w3.org/2005/08/addressing">
      <e:Body><d:ProbeMatches><d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:abc-123</wsa:Address></wsa:EndpointReference>
        <d:Scopes>onvif://www.onvif.org/name/ExampleCam onvif://www.onvif.org/hardware/ACME</d:Scopes>
        <d:XAddrs>http://192.168.1.64/onvif/device_service</d:XAddrs>
      </d:ProbeMatch></d:ProbeMatches></e:Body>
    </e:Envelope>"""
    hit = parse_probe_match(payload, "192.168.1.64")
    assert hit is not None
    assert hit.address == "192.168.1.64" and hit.port == 80
    assert hit.uuid == "urn:uuid:abc-123"
    assert hit.vendor == "ACME"


def test_webhook_signature_is_replay_resistant():
    import time

    from app.notifications.webhook import sign_payload, verify_signature

    body = b'{"id":"e1"}'
    now = int(time.time())
    signature = sign_payload("shared-secret", body, now)
    assert verify_signature("shared-secret", body, now, signature)
    # An old timestamp is rejected even with a valid signature for that time.
    old = now - 3600
    assert not verify_signature("shared-secret", body, old,
                                sign_payload("shared-secret", body, old))


# ==========================================================================
# Timeline density and duplicate detection
# ==========================================================================
def test_timeline_buckets_reflect_coverage(seeded, db_session):
    """Buckets must report real recorded seconds and flag the empty ones."""
    from app.db.models import RecordingSegment
    from app.recording.index import timeline_buckets

    start = dt.datetime(2026, 10, 19, 0, 0, tzinfo=dt.timezone.utc)
    db_session.add(RecordingSegment(
        camera_id="cam-timeline", stream_role="main", relative_path="r/a.mp4",
        started_at=start, ended_at=start + dt.timedelta(hours=1), duration_s=3600,
        size_bytes=1, state="finalized"))
    db_session.commit()

    buckets = timeline_buckets(db_session, "cam-timeline", start,
                               start + dt.timedelta(hours=24), bucket_count=24)

    assert len(buckets) == 24
    assert buckets[0]["recorded_s"] == pytest.approx(3600, abs=1)
    assert buckets[0]["has_gap"] is False
    assert buckets[1]["recorded_s"] == 0
    assert buckets[1]["has_gap"] is True


def test_duplicate_detection_by_serial_mac_and_host(seeded, db_session):
    """Serial and MAC are certain matches; a shared endpoint is only likely."""
    from app.db.models import Camera
    from app.devices.dedupe import find_duplicate

    db_session.add(Camera(name="Existing", host="192.168.5.5", port=80,
                          serial="SN-12345", mac="aabbccddeeff"))
    db_session.commit()

    assert find_duplicate(db_session, serial="SN-12345").confidence == "certain"
    assert find_duplicate(db_session, mac="AA:BB:CC:DD:EE:FF").confidence == "certain"
    assert find_duplicate(db_session, host="192.168.5.5", port=80).confidence == "likely"
    assert find_duplicate(db_session, host="192.168.5.6") is None


def test_builtin_role_permission_sets_are_bounded():
    """A viewer must never silently acquire operator or admin capability."""
    from vms_contracts.enums import Permission

    from app.core.rbac import ADMIN_PERMISSIONS, OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS

    assert Permission.PTZ_CONTROL in OPERATOR_PERMISSIONS
    assert Permission.USER_MANAGE not in OPERATOR_PERMISSIONS
    assert Permission.CAMERA_MANAGE not in VIEWER_PERMISSIONS
    assert Permission.FACE_RECOGNITION_USE not in OPERATOR_PERMISSIONS
    assert len(ADMIN_PERMISSIONS) == len(list(Permission))


def test_onvif_notification_maps_to_camera_edge_analytics():
    """An ONVIF motion event is camera_edge, never server_inference."""
    from vms_contracts.enums import AnalyticsSource

    from app.analytics.ingest import from_onvif_notification

    result = from_onvif_notification("cam1", {
        "topic": "tns1:RuleEngine/CellMotionDetector/Motion",
        "data": {"IsMotion": "true"},
        "utc_time": "2026-10-19T12:00:00Z"})

    assert result is not None
    assert result.source is AnalyticsSource.CAMERA_EDGE
