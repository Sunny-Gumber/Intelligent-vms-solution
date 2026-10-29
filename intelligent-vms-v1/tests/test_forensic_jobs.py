"""Executable admission, authorization, cancellation, isolation and export failure tests."""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import StreamingResponse

from app.core.auth import Principal, get_principal
from app.db.session import get_session
from app.routers import forensic, recordings
from app.services.forensic_jobs import ForensicJobs

IMAGE = "example/worker@sha256:" + "a" * 64
CAMERA = SimpleNamespace(id="camera", tenant_id="tenant", site_id="site")
OWNER = Principal("operator", frozenset({"operator"}), "tenant", frozenset({"site"}))


def manager(tmp_path: Path) -> ForensicJobs:
    """Create a configured isolated registry in an empty private test spool."""
    service = ForensicJobs()
    service.prepare(str(tmp_path), IMAGE)
    return service


def reserve(service: ForensicJobs, subject="operator", tenant="tenant"):
    """Reserve a small request with independent identity and tenant binding."""
    camera = SimpleNamespace(id="camera", tenant_id=tenant, site_id="site")
    return service.reserve(camera, SimpleNamespace(subject=subject), "2026-01-01T00:00:00Z", 2, {})


def test_disabled_and_pinned_configuration(tmp_path):
    service = ForensicJobs()
    for image in ("worker:latest", "https://host/image", "", "image@sha256:bad"):
        with pytest.raises(HTTPException) as error:
            service.prepare(str(tmp_path), image)
        assert error.value.status_code == 503
    with pytest.raises(HTTPException):
        service.prepare("relative", IMAGE)
    manager(tmp_path)
    with pytest.raises(HTTPException, match="503"):
        ForensicJobs().prepare(str(tmp_path), IMAGE)


def test_total_owner_tenant_and_retained_quotas(tmp_path):
    service = manager(tmp_path)
    first = reserve(service)
    with pytest.raises(HTTPException) as error:
        reserve(service)
    assert error.value.status_code == 429
    reserve(service, "second")
    with pytest.raises(HTTPException):
        reserve(service, "third")
    reserve(service, "third", "second-tenant")
    reserve(service, "fourth", "second-tenant")
    with pytest.raises(HTTPException):
        reserve(service, "fifth", "third-tenant")
    service.transition(first, "failed")
    with pytest.raises(HTTPException):
        reserve(service)
    service.delete(first)
    assert len(service.jobs) == 3
    assert not first.directory.exists()


def test_immutable_local_image_id_is_accepted(tmp_path):
    service = ForensicJobs()
    service.prepare(str(tmp_path), "sha256:" + "b" * 64)
    assert service.root == tmp_path.resolve()


def test_container_arguments_are_isolated_and_fixed(tmp_path):
    service = manager(tmp_path)
    job = reserve(service)
    job.settings = {"gamma": 1.2, "roi": (2, 4, 20, 16)}
    arguments = service.arguments(job, IMAGE)
    for option in ("--detach", "--network=none", "--cpus=1", "--memory=512m", "--memory-swap=512m",
                   "--pids-limit=64", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--user=65534:65534", "--log-driver=none", "--pull=never"):
        assert option in arguments
    assert any("/job:" in value and "size=128m" in value for value in arguments)
    assert any("source=" in value and value.endswith(",readonly") for value in arguments)
    assert arguments[-4:] == ["--gamma", "1.2", "--roi", "2,4,20,16"]


def test_validation_rejects_expressions_nonfinite_boolean_and_bad_ranges():
    for values in ({"gamma": "1;rm"}, {"gamma": True}, {"gamma": float("nan")},
                   {"contrast": 4}, {"roi": (0, 0, True, 4)}, {"extra": 1}):
        with pytest.raises(ValidationError):
            forensic.EnhancementOptions(**values)
    with pytest.raises(ValidationError):
        forensic.EnhancementRequest(camera_id="c", start="2026-01-01T00:00:00", duration=2)
    with pytest.raises(ValidationError):
        forensic.EnhancementRequest(camera_id="c", start="2026-01-01T00:00:00Z", duration=float("inf"))


def test_run_completion_hashes_and_cleanup(tmp_path):
    async def scenario():
        service = manager(tmp_path)
        job = reserve(service)
        (job.directory / "original.mp4").write_bytes(b"source")
        calls = []

        async def command(*args, **kwargs):
            calls.append(args)
            if args[0] == "cp":
                target = Path(args[-1])
                if target.suffix == ".mp4":
                    target.write_bytes(b"enhanced")
                else:
                    target.write_text(json.dumps({"source_sha256": hashlib.sha256(b"source").hexdigest(),
                                                 "derivative_sha256": hashlib.sha256(b"enhanced").hexdigest()}))
            return 0

        service.command = command
        service.transition(job, "queued")
        await service.execute(job, IMAGE)
        assert job.state == "completed"
        assert job.cleanup_confirmed
        assert calls[-1] == ("rm", "--force", "vms-forensic-" + job.id)
        assert [event["state"] for event in job.events] == ["staging", "queued", "running", "completed"]
        assert len((job.directory / "audit.jsonl").read_text().splitlines()) == 4
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["run", "cp", "hash", "cleanup"])
def test_execution_failures_never_publish_completed(tmp_path, failure):
    async def scenario():
        service = manager(tmp_path)
        job = reserve(service)

        async def command(*args, **kwargs):
            if args[0] == "rm":
                if failure == "cleanup":
                    raise RuntimeError("cleanup unavailable")
                return 0
            if args[0] == failure:
                raise RuntimeError("synthetic")
            if args[0] == "cp":
                Path(args[-1]).write_bytes(b"bad report")
            return 0

        service.command = command
        await service.execute(job, IMAGE)
        assert job.state == "failed"
        assert job.cleanup_confirmed == (failure != "cleanup")
        if failure == "cleanup":
            with pytest.raises(HTTPException):
                service.delete(job)
            with pytest.raises(HTTPException):
                service.prepare(str(tmp_path), IMAGE)
    asyncio.run(scenario())


def test_queued_cancel_before_first_schedule_is_terminal(tmp_path):
    async def scenario():
        service = manager(tmp_path)
        job = reserve(service)
        service.transition(job, "queued")
        job.task = asyncio.create_task(service.execute(job, IMAGE))
        await service.cancel(job)
        assert job.state == "cancelled"
        assert job.cleanup_confirmed
        await service.cancel(job)
        service.delete(job)
    asyncio.run(scenario())


def test_concurrent_cancel_waits_cleanup_and_holds_worker_slot(tmp_path):
    async def scenario():
        service = manager(tmp_path)
        job = reserve(service)
        running, cleanup_started, cleanup_finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def command(*args, **kwargs):
            if args[0] == "run":
                running.set()
                await asyncio.Event().wait()
            if args[0] == "rm":
                cleanup_started.set()
                await cleanup_finish.wait()
            return 0

        service.command = command
        job.task = asyncio.create_task(service.execute(job, IMAGE))
        await running.wait()
        first_cancel = asyncio.create_task(service.cancel(job))
        await cleanup_started.wait()
        second_cancel = asyncio.create_task(service.cancel(job))
        await asyncio.sleep(0)
        assert service.worker_slot.locked()
        assert job.state == "running"
        assert not second_cancel.done()
        cleanup_finish.set()
        await asyncio.gather(first_cancel, second_cancel)
        assert job.state == "cancelled"
        assert not service.worker_slot.locked()
    asyncio.run(scenario())


def test_reauthorization_owner_and_camera_scope(tmp_path, monkeypatch):
    async def scenario():
        service = manager(tmp_path)
        job = reserve(service)
        monkeypatch.setattr(forensic, "forensic_jobs", service)
        monkeypatch.setattr(forensic, "_enabled", lambda: None)

        async def authorize(session, camera_id, principal):
            assert camera_id == CAMERA.id
            if not principal.can_access(CAMERA.tenant_id, CAMERA.site_id):
                raise HTTPException(404)
            return CAMERA

        monkeypatch.setattr(forensic, "authorized_camera", authorize)
        assert await forensic._authorized_job(job.id, None, OWNER) is job
        outsiders = [Principal("different", OWNER.roles, "tenant", OWNER.site_ids),
                     Principal("operator", OWNER.roles, "other", OWNER.site_ids),
                     Principal("operator", OWNER.roles, "tenant", frozenset({"other"}))]
        for principal in outsiders:
            with pytest.raises(HTTPException):
                await forensic._authorized_job(job.id, None, principal)
        with pytest.raises(HTTPException):
            await forensic._authorized_job("../arbitrary", None, OWNER)
    asyncio.run(scenario())


@pytest.mark.parametrize("status,limit,expected", [(200, 100, None), (206, 100, 502), (200, 1, 413)])
def test_export_staging_strips_range_and_closes_on_all_paths(tmp_path, monkeypatch, status, limit, expected):
    async def scenario():
        closed = []

        async def body():
            try:
                yield b"authorized-export"
            finally:
                closed.append(True)

        async def export(request, *args):
            assert "range" not in request.headers
            return StreamingResponse(body(), status_code=status)

        monkeypatch.setattr(forensic, "stream_recording_clip", export)
        monkeypatch.setattr(forensic, "MAX_SOURCE_BYTES", limit)
        request = Request({"type": "http", "headers": [(b"range", b"bytes=1-2")]})
        payload = forensic.EnhancementRequest(camera_id="camera", start="2026-01-01T00:00:00Z", duration=2)
        job = SimpleNamespace(directory=tmp_path)
        if expected:
            with pytest.raises(HTTPException) as error:
                await forensic._stage(request, CAMERA, payload, None, job)
            assert error.value.status_code == expected
        else:
            await forensic._stage(request, CAMERA, payload, None, job)
            assert (tmp_path / "original.mp4").read_bytes() == b"authorized-export"
        assert closed == [True]
    asyncio.run(scenario())


def test_disabled_api_fails_closed_after_auth(monkeypatch):
    app = FastAPI()
    app.include_router(forensic.router)
    app.dependency_overrides[get_principal] = lambda: OWNER
    app.dependency_overrides[get_session] = lambda: None
    monkeypatch.setattr(forensic.settings, "forensic_jobs_enabled", False)
    response = TestClient(app).get("/api/v1/forensic/jobs/nonexistent")
    assert response.status_code == 503


def test_recording_export_cancellation_releases_semaphore(monkeypatch):
    async def scenario():
        slot = asyncio.Semaphore(1)
        monkeypatch.setattr(recordings, "_export_slots", slot)
        monkeypatch.setattr(recordings.settings, "placement_execution_enabled", False)

        async def policy(*args):
            return SimpleNamespace(record_stream_key="record")

        async def spans(*args):
            return [{"start": "2026-01-01T00:00:00Z", "duration": 5}]

        async def cancelled(*args):
            raise asyncio.CancelledError()

        async def playback(*args):
            return SimpleNamespace(list_timespans=spans, open_stream=cancelled)

        monkeypatch.setattr(recordings, "_policy_for_camera", policy)
        monkeypatch.setattr(recordings, "_playback_for_policy", playback)
        with pytest.raises(asyncio.CancelledError):
            await recordings.stream_recording_clip(Request({"type": "http", "headers": []}), CAMERA,
                                                  datetime(2026, 1, 1, tzinfo=timezone.utc), 2, None)
        assert not slot.locked()
    asyncio.run(scenario())


def test_http_endpoints_recheck_role_owner_tenant_site_and_artifact_binding(tmp_path, monkeypatch):
    service = manager(tmp_path)
    job = reserve(service)
    (job.directory / "original.mp4").write_bytes(b"authorized-original")
    (job.directory / "enhanced.mp4").write_bytes(b"derived")
    (job.directory / "enhanced.mp4.forensic.json").write_text('{"schema":"test"}')
    service.transition(job, "completed")
    monkeypatch.setattr(forensic, "forensic_jobs", service)
    monkeypatch.setattr(forensic, "_enabled", lambda: None)
    identity = [OWNER]

    class Session:
        """Return the real camera entity shape to the actual scope authorization helper."""

        async def get(self, model, identifier):
            return CAMERA if identifier == CAMERA.id else None

    app = FastAPI()
    app.include_router(forensic.router)
    app.dependency_overrides[get_principal] = lambda: identity[0]
    app.dependency_overrides[get_session] = lambda: Session()
    client = TestClient(app)
    prefix = "/api/v1/forensic/jobs/" + job.id
    assert client.get(prefix).status_code == 200
    assert client.get("/api/v1/forensic/jobs").json()["jobs"][0]["id"] == job.id
    assert client.get(prefix + "/report").json()["job"]["id"] == job.id
    assert client.get(prefix + "/artifacts/original").content == b"authorized-original"
    assert client.get(prefix + "/artifacts/enhanced").content == b"derived"
    assert client.get(prefix + "/artifacts/audit.jsonl").status_code == 404
    assert client.get(prefix).headers.get("content-type") == "application/json"
    for outsider, expected in (
        (Principal("operator", frozenset({"viewer"}), "tenant", OWNER.site_ids), 403),
        (Principal("operator", OWNER.roles, "wrong-tenant", OWNER.site_ids), 404),
        (Principal("operator", OWNER.roles, "tenant", frozenset({"wrong-site"})), 404),
        (Principal("other-owner", OWNER.roles, "tenant", OWNER.site_ids), 404),
    ):
        identity[0] = outsider
        listed = client.get("/api/v1/forensic/jobs")
        assert listed.status_code == (403 if expected == 403 else 200)
        if listed.status_code == 200:
            assert listed.json() == {"jobs": []}
        for suffix in ("", "/report", "/artifacts/original", "/artifacts/enhanced"):
            assert client.get(prefix + suffix).status_code == expected
        assert client.post(prefix + "/cancel").status_code == expected
        assert client.delete(prefix).status_code == expected
    identity[0] = OWNER
    assert client.delete(prefix).status_code == 204
    assert client.get(prefix).status_code == 404
    assert client.get("/api/v1/forensic/jobs").json() == {"jobs": []}


def test_json_roi_and_strict_settings_contract():
    payload = forensic.EnhancementRequest.model_validate_json(
        '{"camera_id":"c","start":"2026-01-01T00:00:00Z","duration":2,'
        '"settings":{"roi":[0,0,20,16],"gamma":1.2}}'
    )
    assert payload.settings.roi == (0, 0, 20, 16)
    with pytest.raises(ValidationError):
        forensic.EnhancementRequest.model_validate_json(
            '{"camera_id":"c","start":"2026-01-01T00:00:00Z","duration":2,'
            '"settings":{"gamma":true}}'
        )


def test_http_post_json_roi_stages_only_after_authorization(tmp_path, monkeypatch):
    service = manager(tmp_path)
    monkeypatch.setattr(forensic, "forensic_jobs", service)
    monkeypatch.setattr(forensic, "_enabled", lambda: None)
    staged = []

    class Session:
        """Load one camera for the real tenant/site helper."""

        async def get(self, model, identifier):
            return CAMERA if identifier == CAMERA.id else None

    async def stage(request, camera, payload, session, job):
        staged.append((camera.id, payload.settings.roi))
        (job.directory / "original.mp4").write_bytes(b"export")

    async def execute(job, image):
        service.transition(job, "completed")

    monkeypatch.setattr(forensic, "_stage", stage)
    monkeypatch.setattr(service, "execute", execute)
    app = FastAPI()
    app.include_router(forensic.router)
    app.dependency_overrides[get_session] = lambda: Session()
    payload = {"camera_id": "camera", "start": "2026-01-01T00:00:00Z", "duration": 2,
               "settings": {"roi": [0, 0, 20, 16], "gamma": 1.2}}
    with TestClient(app) as client:
        assert client.post("/api/v1/forensic/jobs", json=payload).status_code == 401
        assert staged == []
        app.dependency_overrides[get_principal] = lambda: OWNER
        response = client.post("/api/v1/forensic/jobs", json=payload)
        assert response.status_code == 202, response.text
        assert staged == [("camera", (0, 0, 20, 16))]
        assert response.json()["settings"]["gamma"] == 1.2
        assert client.post("/api/v1/forensic/jobs", json=payload).status_code == 429
