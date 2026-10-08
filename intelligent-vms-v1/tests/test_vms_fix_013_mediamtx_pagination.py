"""Regression tests for bounded MediaMTX enumeration past the first page.

Upstream MediaMTX v1.21.1 (`internal/api/paginate.go` and `api/openapi.yaml`)
defaults `page` to 0 and `itemsPerPage` to 100. `itemCount` is the total before
pagination and `pageCount` is the number of pages. These tests fail when a
client treats the first page as the complete path set.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import encrypt_secret
from app.db.base import Base
from app.models.entities import CameraEntity, CameraHealthStateEntity, ServiceStateEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.services import health_monitor
from app.services.health_monitor import HealthMonitor
from app.services.mediamtx import MediaMTXClient
from app.services import reconciler

# Pinned upstream defaults. Do not invent different names or page sizes.
UPSTREAM_DEFAULT_PAGE = 0
UPSTREAM_DEFAULT_ITEMS_PER_PAGE = 100


def paginate(items, params):
    """Slice a catalog the way MediaMTX v1.21.1 paginate() does."""
    page = UPSTREAM_DEFAULT_PAGE
    per_page = UPSTREAM_DEFAULT_ITEMS_PER_PAGE
    if params:
        if params.get("page") is not None:
            page = int(params["page"])
        if params.get("itemsPerPage") is not None:
            per_page = int(params["itemsPerPage"])
    start = page * per_page
    total = len(items)
    page_count = 0 if total == 0 else (total + per_page - 1) // per_page
    return {
        "itemCount": total,
        "pageCount": page_count,
        "items": list(items[start : start + per_page]),
    }


class JsonResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class CatalogClient:
    """httpx.AsyncClient stand-in that serves one in-memory MediaMTX catalog."""

    catalog = []
    gets = []
    mutations = []

    def __init__(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        CatalogClient.gets.append({"url": url, "params": params})
        return JsonResponse(paginate(CatalogClient.catalog, params))

    async def post(self, url, json=None, **kwargs):
        CatalogClient.mutations.append(url)
        return JsonResponse({}, status_code=201)

    async def patch(self, url, json=None, **kwargs):
        CatalogClient.mutations.append(url)
        return JsonResponse({}, status_code=200)

    async def delete(self, url, **kwargs):
        CatalogClient.mutations.append(url)
        return JsonResponse({}, status_code=204)


class HostileClient:
    """Always reports a huge itemCount/pageCount so a missing bound would spin."""

    calls = []

    def __init__(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        HostileClient.calls.append({"url": url, "params": params})
        if len(HostileClient.calls) > 20:
            raise AssertionError("MediaMTX enumeration requested more than 20 pages")
        page = UPSTREAM_DEFAULT_PAGE if not params else int(params.get("page", UPSTREAM_DEFAULT_PAGE))
        return JsonResponse(
            {
                "itemCount": 1_000_000_000,
                "pageCount": 10_000_000,
                "items": [{"name": f"hostile-{page}-{index}", "maxReaders": 16} for index in range(100)],
            }
        )


@pytest.fixture(autouse=True)
def _reset_fake_api():
    CatalogClient.catalog = []
    CatalogClient.gets = []
    CatalogClient.mutations = []
    HostileClient.calls = []
    yield


def config_items(count):
    return [
        {"name": f"path-{index:04d}", "maxReaders": settings.live_view_max_readers_per_path}
        for index in range(count)
    ]


def runtime_items(count):
    return [{"name": f"path-{index:04d}", "ready": True, "source": {"type": "rtspSource"}} for index in range(count)]


def camera(stream_key):
    return CameraEntity(
        id=stream_key,
        tenant_id="tenant-a",
        site_id="site-a",
        name=stream_key,
        host="10.1.2.3",
        rtsp_port=554,
        main_path="/main",
        sub_path=None,
        third_path=None,
        third_stream_key=None,
        username_enc=encrypt_secret("admin"),
        password_enc=encrypt_secret("secret"),
        stream_key=stream_key,
        media_node_id="media-a",
        enabled=True,
        desired_state="provisioned",
    )


def test_list_paths_returns_items_beyond_first_upstream_page(monkeypatch):
    CatalogClient.catalog = runtime_items(150)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    result = asyncio.run(MediaMTXClient("http://mediamtx.test:9997").list_paths())
    names = [item["name"] for item in result["items"]]
    assert result["truncated"] is False
    assert result["itemCount"] == 150
    assert result["pageCount"] == 2
    assert len(names) == 150
    assert "path-0000" in names
    assert "path-0149" in names
    assert [call["params"]["page"] for call in CatalogClient.gets] == [0, 1]
    assert [call["params"]["itemsPerPage"] for call in CatalogClient.gets] == [100, 100]


def test_list_config_paths_returns_items_beyond_first_upstream_page(monkeypatch):
    CatalogClient.catalog = config_items(250)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    result = asyncio.run(MediaMTXClient("http://mediamtx.test:9997").list_config_paths())
    names = [item["name"] for item in result["items"]]
    assert result["truncated"] is False
    assert result["itemCount"] == 250
    assert result["pageCount"] == 3
    assert names[0] == "path-0000"
    assert names[-1] == "path-0249"
    assert len(names) == 250
    assert [call["params"]["page"] for call in CatalogClient.gets] == [0, 1, 2]


def test_list_stops_at_configured_bound_and_reports_truncation(monkeypatch):
    CatalogClient.catalog = config_items(500)
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 150)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    result = asyncio.run(MediaMTXClient("http://mediamtx.test:9997").list_config_paths())
    assert result["truncated"] is True
    assert len(result["items"]) == 150
    assert result["itemCount"] == 500
    assert len(CatalogClient.gets) == 2
    assert [call["params"]["page"] for call in CatalogClient.gets] == [0, 1]


def test_list_does_not_loop_when_upstream_page_count_is_unbounded(monkeypatch):
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 250)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", HostileClient)
    result = asyncio.run(MediaMTXClient("http://mediamtx.test:9997").list_paths())
    assert result["truncated"] is True
    assert len(result["items"]) == 250
    assert len(HostileClient.calls) == 3
    assert result["itemCount"] == 1_000_000_000


def test_legacy_reconcile_keeps_paths_beyond_first_page(monkeypatch):
    CatalogClient.catalog = config_items(150)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    cameras = [camera("path-0001"), camera("path-0120")]
    for _run in range(2):
        changed, failed, last = asyncio.run(
            reconciler._legacy_reconcile(cameras, {}, max_changes=10)
        )
        assert changed == 0
        assert failed == 0
        assert last == "path-0120"
    assert CatalogClient.mutations == []


def test_legacy_reconcile_does_not_apply_when_listing_is_truncated(monkeypatch):
    CatalogClient.catalog = config_items(180)
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 50)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    changed, failed, last = asyncio.run(
        reconciler._legacy_reconcile([camera("path-0001"), camera("path-0160")], {}, max_changes=10)
    )
    assert changed == 0
    assert failed == 1
    assert last is None
    assert CatalogClient.mutations == []


def test_distributed_reconcile_keeps_paths_beyond_first_page(monkeypatch):
    CatalogClient.catalog = config_items(150)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    now = datetime.now(timezone.utc)
    node = InfrastructureNodeEntity(
        id="media-a",
        name="media-a",
        region_id="r1",
        roles_json=["media"],
        endpoints_json={"api_url": "http://mediamtx-fix013.test:9997"},
        capacity_json={},
        load_json={},
        state="active",
        enabled=True,
        heartbeat_at=now,
        generation=1,
    )
    row = camera("path-0130")
    assignment = PlacementAssignmentEntity(
        id="pa-media",
        camera_id=row.id,
        role="media",
        region_id="r1",
        node_id="media-a",
        cleanup_node_ids_json=[],
        generation=4,
        applied_generation=4,
        active=True,
        reason="assigned",
        lease_expires_at=now + timedelta(hours=1),
        assigned_at=now,
    )
    changed, failed, last = asyncio.run(
        reconciler._distributed_reconcile(
            [row],
            {},
            {(row.id, "media"): assignment},
            {"media-a": node},
            max_changes=10,
        )
    )
    assert changed == 0
    assert failed == 0
    assert last == row.id
    assert CatalogClient.mutations == []
    assert assignment.applied_generation == 4


def _load_node_agent():
    import importlib.util
    from pathlib import Path

    module_path = Path(__file__).parents[1] / "services" / "node-agent" / "main.py"
    spec = importlib.util.spec_from_file_location("node_agent_fix013", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _probe_catalog():
    items = []
    for index in range(100):
        source = {"type": "rtspSource"} if index < 10 else None
        items.append({"name": f"src-{index:04d}", "source": source})
    for index in range(100, 115):
        items.append({"name": f"src-{index:04d}-record", "source": {"type": "rtspSource"}})
    for index in range(115, 120):
        items.append({"name": f"src-{index:04d}", "source": {"type": "rtspSource"}})
    return items


def test_node_probe_counts_paths_beyond_first_page(monkeypatch):
    node_agent = _load_node_agent()
    agent = node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media,recording",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }))
    catalog = _probe_catalog()
    calls = []

    async def fake_get(url, params=None, **kwargs):
        calls.append(params)
        return JsonResponse(paginate(catalog, params))

    monkeypatch.setattr(agent._client, "get", fake_get)
    result = asyncio.run(agent._probe_mediamtx())
    asyncio.run(agent.close())
    assert result["mediamtx_list_truncated"] == 0.0
    assert result["mediamtx_configured_paths"] == 120.0
    assert result["mediamtx_live_sources"] == 15.0
    assert result["mediamtx_recording_paths"] == 15.0
    assert [call["page"] for call in calls] == [0, 1]


def test_node_probe_truncation_omits_understated_placement_load(monkeypatch):
    node_agent = _load_node_agent()
    agent = node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media,recording",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
        "MEDIAMTX_LIST_MAX_ITEMS": 50,
    }))
    catalog = _probe_catalog()

    async def fake_get(url, params=None, **kwargs):
        return JsonResponse(paginate(catalog, params))

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", fake_get)
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    load = payload["load"]
    assert load["mediamtx_reachable"] == 1.0
    assert load["mediamtx_list_truncated"] == 1.0
    assert "mediamtx_configured_paths" not in load
    assert "mediamtx_live_sources" not in load
    assert "mediamtx_recording_paths" not in load
    assert "active_sources" not in load
    assert "active_recordings" not in load


def _health_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory


async def _prepare_health_db(factory, cameras, health_rows):
    assert ServiceStateEntity.__tablename__ == "service_state"
    async with factory() as session:
        async with session.begin():
            session.add_all(cameras)
            session.add_all(health_rows)


def test_health_monitor_marks_later_page_path_present(monkeypatch):
    CatalogClient.catalog = runtime_items(150)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    monkeypatch.setattr(health_monitor.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_local_store_enabled", False)
    engine, factory = _health_session_factory()

    async def scenario():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(health_monitor, "SessionLocal", factory)

        async def ready(*args, **kwargs):
            return True

        monkeypatch.setattr(health_monitor, "probe_rtsp_transport", ready)
        await _prepare_health_db(factory, [camera("path-0120")], [])
        await HealthMonitor().run_once()
        async with factory() as session:
            row = await session.get(CameraHealthStateEntity, "path-0120")
        return row

    row = asyncio.run(scenario())
    assert row is not None
    assert row.path_present is True
    assert row.detail_json["media_path_present"] is True


def test_health_monitor_does_not_clear_path_present_on_truncation(monkeypatch):
    CatalogClient.catalog = runtime_items(200)
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 50)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    monkeypatch.setattr(health_monitor.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_local_store_enabled", False)
    engine, factory = _health_session_factory()
    before_errors = health_monitor.stats.media_errors
    # SQLite returns naive timestamps. Keep the monitor clock naive too so this
    # test does not depend on the observed_at comparison owned by VMS-FIX-016.
    observed = datetime(2026, 10, 8, 12, 0, 0)

    class NaiveClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed

    monkeypatch.setattr(health_monitor, "datetime", NaiveClock)

    async def scenario():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(health_monitor, "SessionLocal", factory)

        async def ready(*args, **kwargs):
            return True

        monkeypatch.setattr(health_monitor, "probe_rtsp_transport", ready)
        prior = CameraHealthStateEntity(
            camera_id="path-0180",
            state="online",
            path_present=True,
            ready=True,
            failure_count=0,
            success_count=2,
            detail_json={"media_path_present": True},
            observed_at=observed,
            changed_at=observed,
        )
        await _prepare_health_db(factory, [camera("path-0180")], [prior])
        await HealthMonitor().run_once()
        async with factory() as session:
            row = await session.get(CameraHealthStateEntity, "path-0180")
        return row

    row = asyncio.run(scenario())
    assert row.path_present is True
    assert health_monitor.stats.media_errors > before_errors
