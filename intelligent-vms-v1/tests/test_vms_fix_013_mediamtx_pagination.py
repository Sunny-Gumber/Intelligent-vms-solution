"""Regression tests for bounded MediaMTX enumeration past the first page.

Upstream MediaMTX v1.21.1 (`internal/api/paginate.go` and `api/openapi.yaml`)
defaults `page` to 0 and `itemsPerPage` to 100. `itemCount` is the total before
pagination and `pageCount` is the number of pages. These tests fail when a
client treats the first page as the complete path set.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal
from app.core.config import settings
from app.core.security import encrypt_secret
from app.db.base import Base
from app.models.entities import CameraEntity, CameraHealthStateEntity, ServiceStateEntity
from app.models.placement import InfrastructureNodeEntity, PlacementAssignmentEntity
from app.routers import cameras as cameras_router
from app.routers.system import health as system_health
from app.services import health_monitor
from app.services.health_monitor import HealthMonitor
from app.services.mediamtx import MediaMTXClient
from app.services.placement import NodeSnapshot, node_eligible
from app.services import reconciler
from tests.time_control import FIXED_NOW

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
    assert result["inconsistent"] is False
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
    assert result["inconsistent"] is False
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
    assert result["inconsistent"] is False
    assert len(result["items"]) == 150
    assert result["itemCount"] == 500
    assert len(CatalogClient.gets) == 2
    assert [call["params"]["page"] for call in CatalogClient.gets] == [0, 1]


def test_list_does_not_loop_when_upstream_page_count_is_unbounded(monkeypatch):
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 250)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", HostileClient)
    result = asyncio.run(MediaMTXClient("http://mediamtx.test:9997").list_paths())
    assert result["truncated"] is True
    assert result["inconsistent"] is False
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
    assert result["mediamtx_list_inconsistent"] == 0.0
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
    # The stored instant is naive, which is what SQLite returns. The clock
    # honors the timezone argument so the FIX-016 comparison can subtract it
    # from an aware now without this test editing that comparison.
    observed = datetime(2026, 10, 8, 12, 0, 0)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return observed
            return observed.replace(tzinfo=tz)

    monkeypatch.setattr(health_monitor, "datetime", FixedClock)

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


class RaisingResponse:
    """List response whose status check fails the walk."""

    def __init__(self, status_code, url):
        self.status_code = status_code
        self._url = url

    def raise_for_status(self):
        request = httpx.Request("GET", self._url)
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError("server error", request=request, response=response)

    def json(self):
        return {}


class LaterPageFailureClient:
    """Serve page 0 from ``catalog`` and fail every later page."""

    catalog = []
    gets = []
    mutations = []
    failure = "status"

    def __init__(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        page = UPSTREAM_DEFAULT_PAGE if not params else int(params.get("page", UPSTREAM_DEFAULT_PAGE))
        LaterPageFailureClient.gets.append(page)
        if page >= 1:
            if LaterPageFailureClient.failure == "timeout":
                raise httpx.TimeoutException("timed out")
            return RaisingResponse(500, url)
        return JsonResponse(paginate(LaterPageFailureClient.catalog, params))

    async def post(self, url, json=None, **kwargs):
        LaterPageFailureClient.mutations.append(url)
        return JsonResponse({}, status_code=201)

    async def patch(self, url, json=None, **kwargs):
        LaterPageFailureClient.mutations.append(url)
        return JsonResponse({}, status_code=200)

    async def delete(self, url, **kwargs):
        LaterPageFailureClient.mutations.append(url)
        return JsonResponse({}, status_code=204)


class CatalogShiftClient:
    """Change the catalog between page 0 and later pages while reporting page-0 totals.

    ``mode`` is ``delete`` (drop path-0000, which skips path-0100) or ``insert``
    (put path-new at the front, which duplicates path-0099).
    """

    gets = []
    mutations = []
    mode = "delete"

    def __init__(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        page = UPSTREAM_DEFAULT_PAGE if not params else int(params.get("page", UPSTREAM_DEFAULT_PAGE))
        per_page = UPSTREAM_DEFAULT_ITEMS_PER_PAGE if not params else int(
            params.get("itemsPerPage", UPSTREAM_DEFAULT_ITEMS_PER_PAGE)
        )
        CatalogShiftClient.gets.append(page)
        names = [f"path-{index:04d}" for index in range(200)]
        if page == 0:
            chunk = names[:per_page]
        elif CatalogShiftClient.mode == "insert":
            shifted = ["path-new", *names]
            chunk = shifted[page * per_page : page * per_page + per_page]
        else:
            shifted = names[1:]
            chunk = shifted[page * per_page : page * per_page + per_page]
        return JsonResponse(
            {
                "itemCount": 200,
                "pageCount": 2,
                "items": [
                    {"name": name, "maxReaders": settings.live_view_max_readers_per_path}
                    for name in chunk
                ],
            }
        )

    async def post(self, url, json=None, **kwargs):
        CatalogShiftClient.mutations.append(url)
        return JsonResponse({}, status_code=201)

    async def patch(self, url, json=None, **kwargs):
        CatalogShiftClient.mutations.append(url)
        return JsonResponse({}, status_code=200)

    async def delete(self, url, **kwargs):
        CatalogShiftClient.mutations.append(url)
        return JsonResponse({}, status_code=204)


class LookupSession:
    def __init__(self, entity):
        self.entity = entity

    async def get(self, model, key):
        del model, key
        return self.entity


def _operator():
    return Principal(
        subject="operator-1",
        roles=frozenset({"operator"}),
        tenant_id="tenant-a",
        site_ids=frozenset({"site-a"}),
    )


def _walkers():
    from app.services.mediamtx import _enumerate_mediamtx_list as control_walk

    node_agent = _load_node_agent()
    return (("control", control_walk), ("node", node_agent._enumerate_mediamtx_list))


def _names(result):
    return [item["name"] for item in result["items"]]


def _install_fixed_clock(monkeypatch):
    observed = datetime(2026, 10, 8, 12, 0, 0)

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return observed
            return observed.replace(tzinfo=tz)

    monkeypatch.setattr(health_monitor, "datetime", FixedClock)
    return observed


def _prior_health(camera_id, observed):
    return CameraHealthStateEntity(
        camera_id=camera_id,
        state="online",
        path_present=True,
        ready=True,
        failure_count=0,
        success_count=2,
        detail_json={"media_path_present": True},
        observed_at=observed,
        changed_at=observed,
    )


def _reset_failure_clients():
    LaterPageFailureClient.catalog = []
    LaterPageFailureClient.gets = []
    LaterPageFailureClient.mutations = []
    LaterPageFailureClient.failure = "status"
    CatalogShiftClient.gets = []
    CatalogShiftClient.mutations = []
    CatalogShiftClient.mode = "delete"


def test_qa_013_001_later_page_http_500_does_not_publish_spare_capacity(monkeypatch):
    """QA-013-001: a later-page HTTP 500 must not publish active_sources=0."""
    _reset_failure_clients()
    node_agent = _load_node_agent()
    agent = node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media,recording",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }))
    catalog = runtime_items(150)
    calls = []

    async def fake_get(url, params=None, **kwargs):
        page = UPSTREAM_DEFAULT_PAGE if not params else int(params.get("page", UPSTREAM_DEFAULT_PAGE))
        calls.append(page)
        if page >= 1:
            return RaisingResponse(500, url)
        return JsonResponse(paginate(catalog, params))

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", fake_get)
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    assert calls == [0, 1]
    load = payload["load"]
    assert load["mediamtx_reachable"] == 0.0
    assert load["mediamtx_list_failed"] == 1.0
    assert "mediamtx_list_truncated" not in load
    for key in (
        "active_sources",
        "active_recordings",
        "mediamtx_configured_paths",
        "mediamtx_live_sources",
        "mediamtx_recording_paths",
    ):
        assert key not in load
    failed_node = NodeSnapshot(
        id="node-a",
        region_id="region-a",
        roles=frozenset({"media", "recording"}),
        state="active",
        enabled=True,
        capacity={
            "max_sources": 100,
            "max_ingress_mbps": 1000,
            "max_egress_mbps": 1000,
            "max_recordings": 100,
        },
        load=dict(load),
        heartbeat_at=FIXED_NOW,
        authority_mode="central_online",
    )
    assert node_eligible(failed_node, "media", "region-a", FIXED_NOW) is False
    spare_load = dict(load)
    spare_load["active_sources"] = 0
    spare_node = NodeSnapshot(
        id="node-a",
        region_id="region-a",
        roles=frozenset({"media"}),
        state="active",
        enabled=True,
        capacity={"max_sources": 100, "max_ingress_mbps": 1000, "max_egress_mbps": 1000},
        load=spare_load,
        heartbeat_at=FIXED_NOW,
        authority_mode="central_online",
    )
    assert node_eligible(spare_node, "media", "region-a", FIXED_NOW) is True


def test_qa_013_002_later_page_error_keeps_previous_path_present(monkeypatch):
    """QA-013-002: HTTP 500 and timeout on a later page keep the previous flag."""
    _reset_failure_clients()
    observed = _install_fixed_clock(monkeypatch)
    monkeypatch.setattr(health_monitor.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_local_store_enabled", False)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", LaterPageFailureClient)

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(health_monitor, "probe_rtsp_transport", ready)

    async def once(failure):
        LaterPageFailureClient.failure = failure
        LaterPageFailureClient.catalog = runtime_items(150)
        LaterPageFailureClient.gets = []
        engine, factory = _health_session_factory()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(health_monitor, "SessionLocal", factory)
        before_errors = health_monitor.stats.media_errors
        await _prepare_health_db(
            factory,
            [camera("path-0001")],
            [_prior_health("path-0001", observed)],
        )
        await HealthMonitor().run_once()
        async with factory() as session:
            row = await session.get(CameraHealthStateEntity, "path-0001")
        return row, health_monitor.stats.media_errors - before_errors, list(LaterPageFailureClient.gets)

    for failure in ("status", "timeout"):
        row, error_delta, gets = asyncio.run(once(failure))
        assert gets == [0, 1]
        assert row.path_present is True
        assert row.detail_json["media_path_present"] is True
        assert error_delta >= 1


def test_qa_013_002_distributed_node_error_keeps_previous_path_present(monkeypatch):
    observed = _install_fixed_clock(monkeypatch)
    monkeypatch.setattr(health_monitor.settings, "placement_execution_enabled", True)
    monkeypatch.setattr(health_monitor.settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_local_store_enabled", False)

    async def broken_media(node):
        del node

        class Broken:
            async def list_paths(self):
                raise RuntimeError("later page failed")

        return Broken()

    monkeypatch.setattr(health_monitor.node_clients, "media", broken_media)

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(health_monitor, "probe_rtsp_transport", ready)
    engine, factory = _health_session_factory()
    before_errors = health_monitor.stats.media_errors

    async def scenario():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(health_monitor, "SessionLocal", factory)
        node = InfrastructureNodeEntity(
            id="media-a",
            name="media-a",
            region_id="r1",
            roles_json=["media"],
            endpoints_json={"api_url": "http://mediamtx.test:9997"},
            capacity_json={},
            load_json={},
            state="active",
            enabled=True,
            heartbeat_at=FIXED_NOW,
            generation=1,
        )
        await _prepare_health_db(
            factory,
            [camera("path-0001"), node],
            [_prior_health("path-0001", datetime(2026, 10, 8, 12, 0, 0))],
        )
        await HealthMonitor().run_once()
        async with factory() as session:
            return await session.get(CameraHealthStateEntity, "path-0001")

    row = asyncio.run(scenario())
    assert row.path_present is True
    assert health_monitor.stats.media_errors > before_errors


def test_qa_013_003_catalog_shift_does_not_reconcile(monkeypatch):
    """QA-013-003: deletion and insertion between pages are not a complete catalog."""
    _reset_failure_clients()
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogShiftClient)

    for mode, missing in (("delete", "path-0100"), ("insert", "path-new")):
        CatalogShiftClient.mode = mode
        CatalogShiftClient.gets = []
        CatalogShiftClient.mutations = []
        listed = asyncio.run(MediaMTXClient("http://mediamtx.test:9997").list_config_paths())
        assert listed["inconsistent"] is True
        assert listed["truncated"] is False
        assert missing not in _names(listed)
        assert _names(listed).count("path-0099") <= 1
        assert CatalogShiftClient.gets == [0, 1, 0, 1]
        changed, failed, last = asyncio.run(
            reconciler._legacy_reconcile([camera(missing)], {}, max_changes=10)
        )
        assert changed == 0
        assert failed == 1
        assert last is None
        assert CatalogShiftClient.mutations == []
        assert missing not in "".join(CatalogShiftClient.mutations)


def test_qa_013_004_camera_and_system_health_honor_incomplete_walks(monkeypatch):
    """QA-013-004: truncated and inconsistent walks are not path_present false or healthy."""
    monkeypatch.setattr(cameras_router.settings, "placement_execution_enabled", False)
    CatalogClient.catalog = runtime_items(200)
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 50)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    for stream_key in ("path-0001", "path-0180"):
        result = asyncio.run(
            cameras_router.camera_health(stream_key, LookupSession(camera(stream_key)), _operator())
        )
        assert result.path_present is None
        assert result.detail["enumeration"] == "truncated"
    body = asyncio.run(system_health())
    assert body["media_node"] == "degraded"
    assert body["status"] == "degraded"

    _reset_failure_clients()
    CatalogShiftClient.mode = "insert"
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogShiftClient)
    monkeypatch.setattr(settings, "mediamtx_list_max_items", 10000)
    result = asyncio.run(
        cameras_router.camera_health("path-new", LookupSession(camera("path-new")), _operator())
    )
    assert result.path_present is None
    assert result.path_present is not False
    assert result.detail["enumeration"] == "inconsistent"
    body = asyncio.run(system_health())
    assert body["media_node"] == "degraded"
    assert body["status"] == "degraded"

    CatalogClient.catalog = runtime_items(1)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", CatalogClient)
    present = asyncio.run(
        cameras_router.camera_health("path-0000", LookupSession(camera("path-0000")), _operator())
    )
    assert present.path_present is True
    healthy = asyncio.run(system_health())
    assert healthy["media_node"] == "ok"
    assert healthy["status"] == "ok"


def test_qa_013_005_walks_share_body_shape_and_page_count(monkeypatch):
    """QA-013-005: both walks reject the same bad bodies and do not invent pageCount 1."""
    for label, walk in _walkers():
        calls = []

        async def non_object(page, per_page, calls=calls):
            del per_page
            calls.append(page)
            return JsonResponse(["not-an-object"])

        with pytest.raises(Exception):
            asyncio.run(walk(non_object, bound=10000))
        assert calls == [0], label

        calls = []

        async def paths_key(page, per_page, calls=calls):
            del per_page
            calls.append(page)
            return JsonResponse(
                {"paths": [{"name": "path-0000"}], "itemCount": 1, "pageCount": 1}
            )

        with pytest.raises(Exception):
            asyncio.run(walk(paths_key, bound=10000))
        assert calls == [0], label

        calls = []
        pages = {
            0: [{"name": f"a-{index:04d}"} for index in range(100)],
            1: [{"name": f"b-{index:04d}"} for index in range(100)],
            2: [{"name": f"c-{index:04d}"} for index in range(50)],
        }

        async def missing_counts(page, per_page, calls=calls, pages=pages):
            del per_page
            calls.append(page)
            return JsonResponse({"items": pages.get(page, [])})

        result = asyncio.run(walk(missing_counts, bound=10000))
        assert calls == [0, 0], label
        assert result["inconsistent"] is True
        assert result["truncated"] is False
        assert result["pageCount"] is None
        assert len(result["items"]) != 250

    node_agent = _load_node_agent()
    agent = node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }))

    async def fake_get(url, params=None, **kwargs):
        del url, params, kwargs
        return JsonResponse({"paths": [{"name": "path-0000"}], "itemCount": 1, "pageCount": 1})

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", fake_get)
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    assert "active_sources" not in payload["load"]
    assert "mediamtx_configured_paths" not in payload["load"]
    assert payload["load"]["mediamtx_reachable"] == 0.0


def test_qa_013_006_item_count_and_page_count_must_stay_stable():
    """QA-013-006: a page-0 mismatch or a later itemCount change is inconsistent."""
    for label, walk in _walkers():
        calls = []

        async def mismatch(page, per_page, calls=calls):
            del per_page
            calls.append(page)
            assert page == 0
            return JsonResponse(
                {
                    "itemCount": 100,
                    "pageCount": 5,
                    "items": [{"name": f"path-{index:04d}"} for index in range(100)],
                }
            )

        result = asyncio.run(walk(mismatch, bound=10000))
        assert calls == [0, 0], label
        assert result["inconsistent"] is True
        assert result["truncated"] is False
        assert result["itemCount"] == 100
        assert result["pageCount"] == 5

        calls = []

        async def lowered(page, per_page, calls=calls):
            del per_page
            calls.append(page)
            if page == 0:
                return JsonResponse(
                    {
                        "itemCount": 150,
                        "pageCount": 2,
                        "items": [{"name": f"path-{index:04d}"} for index in range(100)],
                    }
                )
            return JsonResponse(
                {
                    "itemCount": 100,
                    "pageCount": 1,
                    "items": [{"name": f"path-{index:04d}"} for index in range(100)],
                }
            )

        result = asyncio.run(walk(lowered, bound=10000))
        assert calls == [0, 1, 0, 1], label
        assert result["inconsistent"] is True
        assert result["truncated"] is False
        assert result["itemCount"] == 150
        assert result["pageCount"] == 2


def test_later_page_timeout_and_http_500_fail_both_walks_without_retry():
    catalog = runtime_items(150)
    for label, walk in _walkers():
        for failure in ("status", "timeout"):
            calls = []

            async def fetch(page, per_page, calls=calls, failure=failure, catalog=catalog):
                del per_page
                calls.append(page)
                if page >= 1:
                    if failure == "timeout":
                        raise httpx.TimeoutException("timed out")
                    return RaisingResponse(500, "http://mediamtx.test/v3/paths/list")
                return JsonResponse(paginate(catalog, {"page": page, "itemsPerPage": 100}))

            with pytest.raises(Exception):
                asyncio.run(walk(fetch, bound=10000))
            assert calls == [0, 1], (label, failure)


def test_legacy_reconcile_does_not_mutate_when_a_later_page_fails(monkeypatch):
    _reset_failure_clients()
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", LaterPageFailureClient)
    for failure in ("status", "timeout"):
        LaterPageFailureClient.failure = failure
        LaterPageFailureClient.catalog = config_items(150)
        LaterPageFailureClient.gets = []
        LaterPageFailureClient.mutations = []
        changed, failed, last = asyncio.run(
            reconciler._legacy_reconcile([camera("path-0100")], {}, max_changes=10)
        )
        assert LaterPageFailureClient.gets == [0, 1]
        assert changed == 0
        assert failed == 1
        assert last is None
        assert LaterPageFailureClient.mutations == []


def test_enumeration_boundaries_agree_on_both_walks():
    node_agent = _load_node_agent()
    assert settings.mediamtx_list_max_items == 10000
    assert node_agent.NodeAgentSettings.model_fields["mediamtx_list_max_items"].default == 10000
    cases = (
        (100, False, 1, 100),
        (101, False, 2, 101),
        (200, False, 2, 200),
        (10000, False, 100, 10000),
        (10001, True, 100, 10000),
    )
    for total, truncated, calls_expected, kept in cases:
        catalog = runtime_items(total)
        expected_pages = (total + UPSTREAM_DEFAULT_ITEMS_PER_PAGE - 1) // UPSTREAM_DEFAULT_ITEMS_PER_PAGE
        for label, walk in _walkers():
            seen = []

            async def fetch(page, per_page, seen=seen, catalog=catalog):
                seen.append(page)
                return JsonResponse(paginate(catalog, {"page": page, "itemsPerPage": per_page}))

            result = asyncio.run(walk(fetch, bound=10000))
            assert result["truncated"] is truncated, (label, total)
            assert result["inconsistent"] is False, (label, total)
            assert result["itemCount"] == total
            assert result["pageCount"] == expected_pages
            assert len(result["items"]) == kept
            assert len(seen) == calls_expected
            assert result["items"][0]["name"] == "path-0000"
            assert result["items"][-1]["name"] == f"path-{kept - 1:04d}"


def _named_paths(count):
    return [{"name": f"path-{index:04d}"} for index in range(count)]


# Upstream counts are reported as given. None means the field was missing or
# was not an exact non-negative int, not that the walk invented a total.
QA_013_101_CASES = (
    ("missing-both-empty", {"items": []}, None, None),
    (
        "itemcount-only",
        {"itemCount": 500, "items": [{"name": "path-0000"}, {"name": "path-0000-main"}]},
        500,
        None,
    ),
    ("pagecount-only", {"pageCount": 5, "items": _named_paths(3)}, None, 5),
    ("negative", {"itemCount": -1, "pageCount": -1, "items": []}, None, None),
    ("bool", {"itemCount": True, "pageCount": False, "items": _named_paths(1)}, None, None),
    ("float", {"itemCount": 500.0, "pageCount": 5.0, "items": _named_paths(1)}, None, None),
    ("numeric-string", {"itemCount": "500", "pageCount": "5", "items": _named_paths(1)}, None, None),
    (
        "short-above-total",
        {"itemCount": 500, "pageCount": 5, "items": [{"name": "path-0000"}, {"name": "path-0000-main"}]},
        500,
        5,
    ),
    ("pagecount-above-short", {"itemCount": 3, "pageCount": 5, "items": _named_paths(3)}, 3, 5),
)


class FixedPageClient:
    """Return one fixed list body for every page."""

    body = {"items": []}
    gets = []
    mutations = []

    def __init__(self, *args, **kwargs):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, params=None, **kwargs):
        del url, kwargs
        page = UPSTREAM_DEFAULT_PAGE if not params else int(params.get("page", UPSTREAM_DEFAULT_PAGE))
        FixedPageClient.gets.append(page)
        return JsonResponse(FixedPageClient.body)

    async def post(self, url, json=None, **kwargs):
        del json, kwargs
        FixedPageClient.mutations.append(url)
        return JsonResponse({}, status_code=201)

    async def patch(self, url, json=None, **kwargs):
        del json, kwargs
        FixedPageClient.mutations.append(url)
        return JsonResponse({}, status_code=200)

    async def delete(self, url, **kwargs):
        del kwargs
        FixedPageClient.mutations.append(url)
        return JsonResponse({}, status_code=204)


def test_qa_013_101_bad_counts_are_not_rewritten_on_either_walk():
    """QA-013-101: a short page must not become a complete catalog."""
    for label, walk in _walkers():
        for name, body, item_count, page_count in QA_013_101_CASES:
            calls = []

            async def fetch(page, per_page, calls=calls, body=body):
                del per_page
                calls.append(page)
                return JsonResponse(body)

            result = asyncio.run(walk(fetch, bound=10000))
            assert calls == [0, 0], (label, name)
            assert result["inconsistent"] is True, (label, name)
            assert result["truncated"] is False, (label, name)
            assert result["itemCount"] == item_count, (label, name, result["itemCount"])
            assert result["pageCount"] == page_count, (label, name, result["pageCount"])
            assert result["pageCount"] != 1 or page_count == 1


def test_valid_empty_catalog_stays_complete_on_either_walk():
    body = {"itemCount": 0, "pageCount": 0, "items": []}
    for label, walk in _walkers():
        calls = []

        async def fetch(page, per_page, calls=calls, body=body):
            del per_page
            calls.append(page)
            return JsonResponse(body)

        result = asyncio.run(walk(fetch, bound=10000))
        assert calls == [0], label
        assert result["inconsistent"] is False
        assert result["truncated"] is False
        assert result["itemCount"] == 0
        assert result["pageCount"] == 0


def test_list_page_contract_is_one_module():
    from pathlib import Path

    from app.services import mediamtx_list_page as shared

    node_agent = _load_node_agent()
    assert Path(node_agent._LIST_PAGE.__file__).resolve() == Path(shared.__file__).resolve()
    dockerfile = Path(__file__).parents[1].joinpath("services", "node-agent", "Dockerfile").read_text()
    assert "services/control-api/app/services/mediamtx_list_page.py" in dockerfile


def test_qa_013_101_empty_items_omits_spare_capacity(monkeypatch):
    node_agent = _load_node_agent()
    agent = node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media,recording",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }))
    calls = []

    async def fake_get(url, params=None, **kwargs):
        del url, kwargs
        page = UPSTREAM_DEFAULT_PAGE if not params else int(params.get("page", UPSTREAM_DEFAULT_PAGE))
        calls.append(page)
        return JsonResponse({"items": []})

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", fake_get)
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    assert calls == [0, 0]
    load = payload["load"]
    assert load["mediamtx_reachable"] == 1.0
    assert load["mediamtx_list_failed"] == 1.0
    for key in (
        "active_sources",
        "active_recordings",
        "mediamtx_configured_paths",
        "mediamtx_live_sources",
        "mediamtx_recording_paths",
    ):
        assert key not in load
    capacity = {
        "max_sources": 100,
        "max_ingress_mbps": 1000,
        "max_egress_mbps": 1000,
        "max_recordings": 100,
    }
    failed_node = NodeSnapshot(
        id="node-a",
        region_id="region-a",
        roles=frozenset({"media", "recording"}),
        state="active",
        enabled=True,
        capacity=capacity,
        load=dict(load),
        heartbeat_at=FIXED_NOW,
        authority_mode="central_online",
    )
    assert node_eligible(failed_node, "media", "region-a", FIXED_NOW) is False
    assert node_eligible(failed_node, "recording", "region-a", FIXED_NOW) is False
    spare = dict(load)
    spare["active_sources"] = 0
    spare["active_recordings"] = 0
    spare_node = NodeSnapshot(
        id="node-a",
        region_id="region-a",
        roles=frozenset({"media", "recording"}),
        state="active",
        enabled=True,
        capacity=capacity,
        load=spare,
        heartbeat_at=FIXED_NOW,
        authority_mode="central_online",
    )
    assert node_eligible(spare_node, "media", "region-a", FIXED_NOW) is True
    assert node_eligible(spare_node, "recording", "region-a", FIXED_NOW) is True


def test_qa_013_101_short_page_does_not_publish_partial_load(monkeypatch):
    node_agent = _load_node_agent()
    agent = node_agent.NodeAgent(node_agent.NodeAgentSettings.model_validate({
        "NODE_ID": "node-a",
        "REGION_ID": "region-a",
        "NODE_ROLES": "media",
        "CONTROL_API_URL": "http://control-api:8000",
        "NODE_AGENT_TOKEN": "secret-token",
        "MEDIAMTX_API_URL": "http://mediamtx:9997",
    }))

    async def fake_get(url, params=None, **kwargs):
        del url, params, kwargs
        return JsonResponse({"pageCount": 5, "items": _named_paths(3)})

    monkeypatch.setattr(agent, "_measure_host", lambda: {"net_rx_bps": 0.0, "net_tx_bps": 0.0})
    monkeypatch.setattr(agent._client, "get", fake_get)
    payload = asyncio.run(agent.build_heartbeat_payload())
    asyncio.run(agent.close())
    load = payload["load"]
    assert "active_sources" not in load
    assert load.get("mediamtx_live_sources") != 3.0
    assert "mediamtx_live_sources" not in load
    assert load["mediamtx_list_failed"] == 1.0


def test_qa_013_101_bad_counts_do_not_reconcile_or_clear_health(monkeypatch):
    """A rewritten short page must not add, delete, or clear path_present."""
    observed = _install_fixed_clock(monkeypatch)
    monkeypatch.setattr(health_monitor.settings, "placement_execution_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_pipeline_enabled", False)
    monkeypatch.setattr(health_monitor.settings, "event_local_store_enabled", False)
    monkeypatch.setattr(cameras_router.settings, "placement_execution_enabled", False)
    monkeypatch.setattr("app.services.mediamtx.httpx.AsyncClient", FixedPageClient)

    async def ready(*args, **kwargs):
        return True

    monkeypatch.setattr(health_monitor, "probe_rtsp_transport", ready)
    bodies = (
        {"items": []},
        {"itemCount": 500, "items": [{"name": "path-0000"}, {"name": "path-0000-main"}]},
        {"pageCount": 5, "items": _named_paths(3)},
    )
    for body in bodies:
        FixedPageClient.body = body
        FixedPageClient.gets = []
        FixedPageClient.mutations = []
        changed, failed, last = asyncio.run(
            reconciler._legacy_reconcile(
                [camera("path-0000"), camera("path-0100")],
                {},
                max_changes=10,
            )
        )
        assert changed == 0
        assert failed == 1
        assert last is None
        assert FixedPageClient.mutations == []
        assert FixedPageClient.gets == [0, 0]

        engine, factory = _health_session_factory()
        before_errors = health_monitor.stats.media_errors

        async def once(factory=factory):
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            monkeypatch.setattr(health_monitor, "SessionLocal", factory)
            await _prepare_health_db(
                factory,
                [camera("path-0120")],
                [_prior_health("path-0120", observed)],
            )
            await HealthMonitor().run_once()
            async with factory() as session:
                return await session.get(CameraHealthStateEntity, "path-0120")

        row = asyncio.run(once())
        assert row.path_present is True
        assert row.detail_json["media_path_present"] is True
        assert health_monitor.stats.media_errors > before_errors
        result = asyncio.run(
            cameras_router.camera_health("path-0120", LookupSession(camera("path-0120")), _operator())
        )
        assert result.path_present is None
        assert result.path_present is not False
        assert result.detail["enumeration"] == "inconsistent"
        system = asyncio.run(system_health())
        assert system["status"] == "degraded"
        assert system["media_node"] == "degraded"
