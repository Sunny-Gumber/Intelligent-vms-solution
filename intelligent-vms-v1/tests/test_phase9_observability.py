import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import observability
from app.routers import system
from app.services.operational_metrics import OperationalSnapshot


def test_route_label_uses_template_not_raw_resource_id():
    request = SimpleNamespace(
        scope={"route": SimpleNamespace(path="/api/v1/cameras/{camera_id}")},
        url=SimpleNamespace(path="/api/v1/cameras/secret-camera-id"),
    )
    assert observability.route_label(request) == "/api/v1/cameras/{camera_id}"


def test_route_label_bounds_unknown_paths_to_one_label():
    request = SimpleNamespace(
        scope={},
        url=SimpleNamespace(path="/attacker/random/resource/123"),
    )
    assert observability.route_label(request) == "__unmatched__"


class _Session:
    def __init__(self, *, fail=False):
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, _statement):
        if self.fail:
            raise RuntimeError("database unavailable")
        return None


def test_readiness_passes_when_database_query_succeeds(monkeypatch):
    monkeypatch.setattr(system, "SessionLocal", lambda: _Session())
    result = asyncio.run(system.readiness())
    assert result == {"status": "ready", "database": "ok"}


def test_readiness_returns_503_when_database_is_unavailable(monkeypatch):
    monkeypatch.setattr(system, "SessionLocal", lambda: _Session(fail=True))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(system.readiness())
    assert exc.value.status_code == 503


def test_metrics_export_reconciler_and_bounded_outbox_states(monkeypatch):
    async def fake_counts():
        return {"pending": 2, "retry": 3, "dead": 1, "delivered": 7}

    async def fake_snapshot():
        return OperationalSnapshot()

    monkeypatch.setattr(observability, "outbox_counts", fake_counts)
    monkeypatch.setattr(observability, "collect_operational_snapshot", fake_snapshot)
    monkeypatch.setattr(observability.reconciler_stats, "total_runs", 11)
    monkeypatch.setattr(observability.reconciler_stats, "last_duration_seconds", 0.25)
    monkeypatch.setattr(observability.reconciler_stats, "last_scanned", 100)
    monkeypatch.setattr(observability.reconciler_stats, "last_changed", 4)
    monkeypatch.setattr(observability.reconciler_stats, "last_failed", 1)

    response = asyncio.run(observability.metrics())
    body = response.body.decode("utf-8")

    assert "intelligent_vms_reconciler_total_runs 11.0" in body
    assert 'intelligent_vms_outbox_items{status="pending"} 2.0' in body
    assert 'intelligent_vms_outbox_items{status="retry"} 3.0' in body
    assert 'intelligent_vms_outbox_items{status="dead"} 1.0' in body


def test_metrics_export_bounded_operational_snapshot(monkeypatch):
    """Export aggregate camera, recording, node, placement and AI metrics."""

    async def fake_counts():
        return {"pending": 2, "retry": 1, "dead": 0, "delivered": 7}

    async def fake_snapshot():
        return OperationalSnapshot(
            camera_states={
                "online": 8,
                "degraded": 1,
                "offline": 2,
                "unknown": 3,
                "other": 0,
            },
            camera_transport_ready=9,
            camera_health_stale=1,
            media_paths_present=10,
            recording_active=7,
            recording_gap_candidates=2,
            recording_health_untracked=1,
            node_states={"active": 3, "draining": 1, "maintenance": 0, "other": 0},
            node_authority_modes={
                "central_online": 3,
                "regional_autonomous": 1,
                "fenced_degraded": 0,
                "other": 0,
            },
            node_stale=1,
            node_saturated={"media": 1, "recording": 0, "ai": 0},
            node_capacity_unmeasured={"media": 0, "recording": 1, "ai": 1},
            placement_active={"media": 10, "recording": 7, "ai": 2},
            placement_unapplied={"media": 0, "recording": 1, "ai": 2},
            ai_policies_enabled=2,
            outbox_oldest_pending_age_seconds=42.5,
        )

    monkeypatch.setattr(observability, "outbox_counts", fake_counts)
    monkeypatch.setattr(observability, "collect_operational_snapshot", fake_snapshot)

    body = asyncio.run(observability.metrics()).body.decode("utf-8")

    assert 'intelligent_vms_camera_health{state="offline"} 2.0' in body
    assert "intelligent_vms_recording_gap_candidates 2.0" in body
    assert "intelligent_vms_recording_health_untracked 1.0" in body
    assert 'intelligent_vms_node_saturated{role="media"} 1.0' in body
    assert (
        'intelligent_vms_placement_assignments{role="recording",status="unapplied"} 1.0'
        in body
    )
    assert "intelligent_vms_ai_policies_enabled 2.0" in body
    assert "intelligent_vms_outbox_oldest_pending_age_seconds 42.5" in body
    assert "intelligent_vms_operational_refresh_ok 1.0" in body
    assert "intelligent_vms_ready 1.0" in body


def test_metrics_remain_scrapeable_when_outbox_database_refresh_fails(monkeypatch):
    async def fail_counts():
        raise RuntimeError("postgres unavailable")

    async def fake_snapshot():
        return OperationalSnapshot()

    monkeypatch.setattr(observability, "outbox_counts", fail_counts)
    monkeypatch.setattr(observability, "collect_operational_snapshot", fake_snapshot)
    response = asyncio.run(observability.metrics())
    body = response.body.decode("utf-8")

    assert response.status_code == 200
    assert "intelligent_vms_ready 0.0" in body
    assert 'intelligent_vms_metrics_refresh_errors_total{subsystem="outbox"}' in body



def test_production_web_uses_same_origin_api_proxy():
    from pathlib import Path

    root = Path(__file__).parents[1]
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    template = (
        root / "services" / "web" / "default.conf.template"
    ).read_text(encoding="utf-8")

    assert "const API='/api/v1';" in index
    assert "http://localhost:8000/api/v1" not in index
    assert "proxy_pass ${CONTROL_API_UPSTREAM};" in template
