from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.routers.events import _validate_window

ROOT=Path(__file__).parents[1]
ROUTER=(ROOT/"services/control-api/app/routers/events.py").read_text(encoding="utf-8")
ENTITIES=(ROOT/"services/control-api/app/models/entities.py").read_text(encoding="utf-8")
MIGRATION=(ROOT/"migrations/versions/0018_local_event_history.py").read_text(encoding="utf-8")
WINDOWS=(ROOT/"deploy/windows/generate_windows_env.py").read_text(encoding="utf-8")
SECURITY=(ROOT/"services/control-api/app/core/security_posture.py").read_text(encoding="utf-8")
EVENT_SEARCH=(ROOT/"services/control-api/app/services/event_search.py").read_text(encoding="utf-8")


def test_event_window_defaults_are_bounded_and_timezone_aware():
    start,end=_validate_window(None,None)
    assert start.tzinfo is not None and end.tzinfo is not None
    assert timedelta(minutes=59) <= end-start <= timedelta(hours=1,minutes=1)


def test_event_window_rejects_naive_invalid_and_excessive_ranges():
    aware=datetime(2026,10,4,tzinfo=timezone.utc)
    with pytest.raises(HTTPException):
        _validate_window(datetime(2026,10,4),aware)
    with pytest.raises(HTTPException):
        _validate_window(aware,aware)
    with pytest.raises(HTTPException):
        _validate_window(aware,aware+timedelta(days=8))


def test_reduced_profile_event_history_is_postgres_not_enterprise_bus():
    assert '"EVENT_PIPELINE_ENABLED": "false"' in WINDOWS
    assert '"EVENT_HISTORY_ENABLED": "true"' in WINDOWS
    assert '"EVENT_LOCAL_STORE_ENABLED": "true"' in WINDOWS
    assert '"CLICKHOUSE_URL": ""' in WINDOWS
    assert "event_local_store_enabled" in SECURITY
    assert "class EventHistoryEntity" in ENTITIES
    assert 'down_revision: Union[str, Sequence[str], None] = "0017"' in MIGRATION


def test_event_history_is_deterministic_bounded_and_paged():
    assert '@router.get("/history"' in ROUTER
    assert "limit: int = Query(default=100, ge=1, le=200)" in ROUTER
    assert "EventHistoryEntity.timestamp.desc(), EventHistoryEntity.event_id.desc()" in ROUTER
    assert "next_before" in ROUTER and "next_before_id" in ROUTER
    assert "before_timestamp" in EVENT_SEARCH and "before_event_id" in EVENT_SEARCH


def test_event_history_reauthorizes_camera_and_scopes_server_side():
    assert "authorized_camera(session, camera_id, principal)" in ROUTER
    assert "require_scope(" in ROUTER
    assert "camera.tenant_id != event.tenant_id or camera.site_id != event.site_id" in ROUTER
    assert 'require_roles("admin", "operator", "viewer")' in ROUTER


def test_event_history_dedupes_by_authoritative_id_and_has_retention():
    assert "event_id: Mapped[str] = mapped_column(String(128), primary_key=True)" in ENTITIES
    assert "session.get(EventHistoryEntity, event.event_id)" in ROUTER
    assert "event_local_retention_days" in ROUTER
    assert "delete(EventHistoryEntity)" in ROUTER


def test_generic_event_acknowledgement_is_not_faked():
    assert "acknowledge" not in ROUTER.lower()
    client=(ROOT/"clients/windows/src/IntelligentVMS.Desktop/EventCenter.cs").read_text(encoding="utf-8")
    xaml=(ROOT/"clients/windows/src/IntelligentVMS.Desktop/MainWindow.xaml").read_text(encoding="utf-8")
    assert "IsAcknowledged" not in client
    assert "Generic event acknowledgement is not exposed" in xaml


def test_event_center_history_contract_excludes_snapshot_urls():
    schemas=(ROOT/"services/control-api/app/models/schemas.py").read_text(encoding="utf-8")
    block=schemas.split("class EventCenterRead",1)[1].split("class EventHistoryPage",1)[0]
    assert "snapshot_uri" not in block
    assert 'exclude={"snapshot_uri"}' in ROUTER


def test_event_response_and_client_do_not_use_raw_camera_transport():
    client=(ROOT/"clients/windows/src/IntelligentVMS.Desktop/EventCenter.cs").read_text(encoding="utf-8")
    for forbidden in ("rtsp://","password","Authorization","snapshot_uri"):
        assert forbidden not in client
