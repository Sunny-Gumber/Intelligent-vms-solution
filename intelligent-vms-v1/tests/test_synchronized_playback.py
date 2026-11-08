"""Exercise synchronized recording coverage through the mounted HTTP API."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models.entities import CameraEntity, RecordingPolicyEntity
from app.routers import recordings
from app.services.playback import PlaybackClient, PlaybackError
from app.services.search_page import RecordingIndexPage


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=10)
ENDPOINT = "/api/v1/recordings/synchronized"
SECRET = "synthetic-synchronized-playback-test-secret"


def _at(minute):
    return START + timedelta(minutes=minute)


def _window(left, right):
    return {"start": _at(left).isoformat().replace("+00:00", "Z"),
            "end": _at(right).isoformat().replace("+00:00", "Z")}


def _page(spans=(), *, skipped=0, exhausted=True):
    return RecordingIndexPage(
        [{"segment_start": _at(left), "segment_end": _at(right)} for left, right in spans],
        skipped_rows=skipped, exhausted=exhausted,
    )


@asynccontextmanager
async def _client(monkeypatch, *, distributed=True, role="viewer"):
    monkeypatch.setattr(settings, "placement_execution_enabled", distributed)
    monkeypatch.setattr(settings, "auth_disabled", False)
    monkeypatch.setattr(settings, "auth_jwks_url", "")
    monkeypatch.setattr(settings, "auth_issuer", "")
    monkeypatch.setattr(settings, "auth_hs256_secret", SECRET)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def session_dependency():
        async with sessions() as session:
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_session, session_dependency)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as session:
            for camera_id, tenant, site in (
                ("cam-a", "tenant-a", "site-a"),
                ("cam-b", "tenant-a", "site-a"),
                ("cam-unconfigured", "tenant-a", "site-a"),
                ("cam-site", "tenant-a", "site-b"),
                ("cam-tenant", "tenant-b", "site-a"),
            ):
                session.add(CameraEntity(
                    id=camera_id, tenant_id=tenant, site_id=site, name=camera_id,
                    host="192.0.2.1", main_path="/synthetic", stream_key=f"live-{camera_id}",
                ))
                if camera_id != "cam-unconfigured":
                    session.add(RecordingPolicyEntity(
                        camera_id=camera_id, record_stream_key=f"record-{camera_id}",
                    ))
        headers = {}
        if role:
            token = jwt.encode({
                "sub": "fixture-viewer", "roles": [role], "tenant_id": "tenant-a",
                "site_ids": ["site-a"], "aud": settings.auth_audience,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            }, SECRET, algorithm="HS256")
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers,
        ) as client:
            yield client
    finally:
        await engine.dispose()


async def _get(client, camera_ids=("cam-a", "cam-b"), **kwargs):
    params = {"camera_ids": camera_ids, "start": START.isoformat(), "end": END.isoformat()}
    params.update(kwargs)
    return await client.get(ENDPOINT, params=params)


def test_indexed_coverage_clips_merges_and_preserves_real_gaps(monkeypatch):
    index = AsyncMock(side_effect=[
        _page([(6, 11), (-1, 2), (2, 4), (1, 3)]),
        _page([(2, 5), (6, 9)]),
    ])
    monkeypatch.setattr(recordings.recording_index, "segments", index)

    async def scenario():
        async with _client(monkeypatch) as client:
            response = await _get(client, start="2026-10-01T05:30:00+05:30")
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            **_window(0, 10),
            "tracks": [
                {"camera_id": "cam-a", "recorded": [_window(0, 4), _window(6, 10)],
                 "gaps": [_window(4, 6)]},
                {"camera_id": "cam-b", "recorded": [_window(2, 5), _window(6, 9)],
                 "gaps": [_window(0, 2), _window(5, 6), _window(9, 10)]},
            ],
            "common_coverage": [_window(2, 4), _window(6, 9)],
        }
        assert [call.kwargs["camera_id"] for call in index.await_args_list] == ["cam-a", "cam-b"]
        for call in index.await_args_list:
            assert call.kwargs["tenant_id"] == "tenant-a"
            assert call.kwargs["site_id"] == "site-a"
            assert call.kwargs["start"] == START
            assert 1 <= call.kwargs["limit"] <= 5000

    asyncio.run(scenario())


@pytest.mark.parametrize(("role", "ids", "extra", "status"), [
    (None, ("cam-a",), {}, 401),
    ("service", ("cam-a",), {}, 403),
    ("viewer", ("cam-a", "cam-site"), {}, 404),
    ("viewer", ("cam-a", "cam-tenant"), {}, 404),
    ("viewer", ("cam-a", "cam-missing"), {}, 404),
    ("viewer", ("cam-a",), {"role": "admin", "tenant_id": "tenant-b",
                             "subject_id": "fake", "segments_by_camera": "{}"}, 422),
    ("viewer", ("cam-a", "cam-a"), {}, 422),
    ("viewer", ("cam-a",) * 17, {}, 422),
    ("viewer", ("cam-a",), {"start": "2026-10-01T00:00:00"}, 422),
    ("viewer", ("cam-a",), {"end": START.isoformat()}, 422),
    ("viewer", ("cam-a",), {"end": "2027-01-01T00:00:00Z"}, 422),
])
def test_rejected_requests_never_read_the_recording_index(monkeypatch, role, ids, extra, status):
    index = AsyncMock()
    monkeypatch.setattr(recordings.recording_index, "segments", index)

    async def scenario():
        async with _client(monkeypatch, role=role) as client:
            response = await _get(client, camera_ids=ids, **extra)
        assert response.status_code == status, response.text
        index.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize(("page", "status"), [
    (_page([], skipped=1), 503),
    (_page([(1, 2)], exhausted=False), 422),
])
def test_incomplete_index_never_claims_missing_footage(monkeypatch, page, status):
    monkeypatch.setattr(recordings.recording_index, "segments", AsyncMock(return_value=page))

    async def scenario():
        async with _client(monkeypatch) as client:
            response = await _get(client)
        assert response.status_code == status, response.text
        assert "tracks" not in response.json()

    asyncio.run(scenario())


def test_unconfigured_camera_has_full_gap_and_no_index_lookup(monkeypatch):
    index = AsyncMock()
    monkeypatch.setattr(recordings.recording_index, "segments", index)

    async def scenario():
        async with _client(monkeypatch) as client:
            response = await _get(client, camera_ids=("cam-unconfigured",))
        assert response.status_code == 200, response.text
        assert response.json()["tracks"] == [
            {"camera_id": "cam-unconfigured", "recorded": [], "gaps": [_window(0, 10)]},
        ]
        assert response.json()["common_coverage"] == []
        index.assert_not_awaited()

    asyncio.run(scenario())


def test_single_node_uses_policy_path_and_verified_empty_coverage(monkeypatch):
    playback = AsyncMock(side_effect=[
        [{"start": _at(1).isoformat(), "duration": 120}],
        [],
    ])
    monkeypatch.setattr(recordings.playback_client, "list_timespans", playback)
    index = AsyncMock()
    monkeypatch.setattr(recordings.recording_index, "segments", index)

    async def scenario():
        async with _client(monkeypatch, distributed=False) as client:
            response = await _get(client)
        assert response.status_code == 200, response.text
        assert response.json()["tracks"][1] == {
            "camera_id": "cam-b", "recorded": [], "gaps": [_window(0, 10)],
        }
        assert response.json()["common_coverage"] == []
        assert [call.args[0] for call in playback.await_args_list] == ["record-cam-a", "record-cam-b"]
        assert all(call.kwargs["empty_if_no_segments"] for call in playback.await_args_list)
        index.assert_not_awaited()

    asyncio.run(scenario())


def test_single_node_malformed_timeline_fails_instead_of_fabricating_gaps(monkeypatch):
    monkeypatch.setattr(recordings.playback_client, "list_timespans", AsyncMock(
        return_value=[{"start": "malformed", "duration": 10}],
    ))

    async def scenario():
        async with _client(monkeypatch, distributed=False) as client:
            response = await _get(client)
        assert response.status_code == 502, response.text
        assert "tracks" not in response.json()

    asyncio.run(scenario())


@pytest.mark.parametrize(("error", "empty_if_no_segments", "is_empty"), [
    ("no recording segments found", True, True),
    ("no recording segments found", False, False),
    ("proxy endpoint not found", True, False),
])
def test_playback_client_only_maps_known_empty_response(monkeypatch, error, empty_if_no_segments, is_empty):
    client_type = httpx.AsyncClient

    def playback_response(request):
        assert request.url.path == "/list"
        assert request.url.params["path"] == "record-cam-a"
        return httpx.Response(404, json={"status": "error", "error": error})

    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(playback_response), **kwargs),
    )
    request = PlaybackClient("http://playback.test").list_timespans(
        "record-cam-a", START, END, empty_if_no_segments=empty_if_no_segments,
    )
    if is_empty:
        assert asyncio.run(request) == []
    else:
        with pytest.raises(PlaybackError):
            asyncio.run(request)
