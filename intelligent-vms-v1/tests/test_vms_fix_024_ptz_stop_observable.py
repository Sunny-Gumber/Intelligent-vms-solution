"""F24: a failed compensating PTZ STOP must be observable and must not leak secrets.

The camera is a fake. The exception message carries a synthetic password and URL
so the test can prove those values never reach the log, the metric, or the
HTTP response.
"""

import asyncio
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from prometheus_client import REGISTRY

from app.core.auth import Principal
from app.models.schemas import PtzMoveRequest
from app.routers import ptz as ptz_router

CAMERA_ID = "camera-fix-024"
SECRET = "fake-password-s3cret"
DEVICE_URL = "http://203.0.113.50/onvif/ptz?user=cam&password=fake-password-s3cret"
METRIC_NAME = "intelligent_vms_ptz_compensating_stop_failures_total"
EVENT_NAME = "ptz_compensating_stop_failed"


class _FakeStopFailure(RuntimeError):
    """Stand-in device failure whose message is intentionally sensitive."""


def _principal() -> Principal:
    return Principal(
        subject="operator-fix-024",
        roles=frozenset({"operator"}),
        tenant_id="tenant-a",
        site_ids=frozenset({"site-a"}),
    )


def _reset_ptz_state() -> None:
    ptz_router._generations.clear()
    ptz_router._last_move_at.clear()
    ptz_router._context_seen_at.clear()


def _metric_value() -> float:
    value = REGISTRY.get_sample_value(METRIC_NAME)
    return 0.0 if value is None else float(value)


def _metric_text() -> str:
    lines: list[str] = []
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            if sample.name != METRIC_NAME:
                continue
            lines.append(f"{sample.name} {sample.labels!r} {sample.value}")
    return "\n".join(lines)


def _chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(repr(current))
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return "\n".join(parts)


def _assert_no_secrets(*chunks: str) -> None:
    for chunk in chunks:
        assert SECRET not in chunk
        assert DEVICE_URL not in chunk
        assert "203.0.113.50" not in chunk


async def _superseded_move(monkeypatch, stop_impl, stop_calls: dict):
    actor = _principal()
    context_id = uuid4()
    generation = 4
    camera = SimpleNamespace(id=CAMERA_ID, tenant_id="tenant-a", site_id="site-a")
    capability = SimpleNamespace(services_json=[], main_profile_token="profile-main")

    async def fake_context(_camera_id, _session, _principal_arg):
        return camera, capability, "cam-user", SECRET

    async def fake_move(*_args, **_kwargs):
        key = (actor.subject, CAMERA_ID, str(context_id))
        async with ptz_router._generation_lock:
            ptz_router._generations[key] = generation + 1

    async def fake_stop(*args, **kwargs):
        stop_calls["n"] += 1
        stop_calls["password"] = args[3] if len(args) > 3 else kwargs.get("password")
        await stop_impl()

    monkeypatch.setattr(ptz_router, "_context", fake_context)
    monkeypatch.setattr(ptz_router.ptz_service, "continuous_move", fake_move)
    monkeypatch.setattr(ptz_router.ptz_service, "stop", fake_stop)
    payload = PtzMoveRequest(
        pan=0.25,
        tilt=0.0,
        zoom=0.0,
        generation=generation,
        context_id=context_id,
    )
    await ptz_router.move_camera(CAMERA_ID, payload, session=None, principal=actor)


def test_compensating_stop_failure_is_logged_without_secrets(monkeypatch, caplog):
    """A forced STOP failure keeps the 409 and records only safe identifiers."""

    async def fail_stop():
        raise _FakeStopFailure(f"device rejected STOP at {DEVICE_URL} password={SECRET}")

    _reset_ptz_state()
    before = _metric_value()
    stop_calls = {"n": 0, "password": None}
    with caplog.at_level(logging.ERROR, logger="app.routers.ptz"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(_superseded_move(monkeypatch, fail_stop, stop_calls))

    error = raised.value
    assert error.status_code == 409
    assert error.detail == {
        "code": "STALE_PTZ_COMMAND",
        "message": "PTZ movement was superseded",
    }
    assert stop_calls["n"] == 1
    assert stop_calls["password"] == SECRET

    failure_logs = [
        record
        for record in caplog.records
        if record.name == "app.routers.ptz" and EVENT_NAME in record.getMessage()
    ]
    assert len(failure_logs) == 1
    message = failure_logs[0].getMessage()
    assert f"camera_id={CAMERA_ID}" in message
    assert "operation=compensating_stop" in message
    assert "error_class=_FakeStopFailure" in message
    assert failure_logs[0].levelno == logging.ERROR
    assert failure_logs[0].exc_info is None
    assert all(SECRET not in str(arg) and DEVICE_URL not in str(arg) for arg in failure_logs[0].args)

    metric_text = _metric_text()
    assert _metric_value() == before + 1.0
    assert METRIC_NAME in metric_text
    assert CAMERA_ID not in metric_text
    assert "_FakeStopFailure" not in metric_text
    _assert_no_secrets(message, caplog.text, _chain_text(error), str(error.detail), metric_text)


def test_successful_compensating_stop_keeps_stale_response_and_does_not_count(monkeypatch, caplog):
    """A completed compensation still reports the superseded MOVE and stays quiet."""

    async def succeed_stop():
        return None

    _reset_ptz_state()
    before = _metric_value()
    stop_calls = {"n": 0, "password": None}
    with caplog.at_level(logging.ERROR, logger="app.routers.ptz"):
        with pytest.raises(HTTPException) as raised:
            asyncio.run(_superseded_move(monkeypatch, succeed_stop, stop_calls))

    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == "STALE_PTZ_COMMAND"
    assert stop_calls["n"] == 1
    assert EVENT_NAME not in caplog.text
    assert _metric_value() == before
    _assert_no_secrets(caplog.text, _chain_text(raised.value), _metric_text())
