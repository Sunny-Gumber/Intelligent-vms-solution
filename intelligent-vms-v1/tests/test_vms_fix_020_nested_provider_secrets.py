"""F20: nested AI provider config must reject secret-like keys and hide legacy values.

These tests use the public validator and the policy readback path that exist on
unmodified main. Secret fixtures are synthetic markers, never real credentials.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import Principal, get_principal
from app.core.errors import install_error_handlers
from app.db.base import Base
from app.db.session import get_session
from app.models.entities import CameraAIPolicyEntity, CameraEntity
from app.routers.ai import policy_read, router
from app.services.ai import validate_provider_config

SECRET = "synthetic-provider-secret-value"
CAMERA_ID = "cam-fix-020"
WHEN = datetime(2026, 10, 8, tzinfo=timezone.utc)


def _message(exc: BaseException) -> str:
    text = str(exc)
    assert SECRET not in text
    return text


def _policy_row(config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        camera_id=CAMERA_ID,
        enabled=False,
        source_mode="inference",
        model_id=None,
        stream_role="sub",
        sample_fps=1.0,
        min_confidence=0.5,
        analytics_json=[],
        zones_json=[],
        provider_config_json=config,
        updated_at=WHEN,
    )


def _legacy_config() -> dict:
    return {
        "batch_size": 4,
        "runtime": {"endpoint": "https://example.invalid/infer", "api_key": SECRET},
        "workers": [{"name": "primary", "credential": SECRET}],
        "extra": [{"nested": [{"token": SECRET}]}],
    }


@pytest.mark.parametrize(
    ("config", "path_bits"),
    [
        ({"runtime": {"api_key": SECRET}}, ("runtime", "api_key")),
        ({"runtime": {"API_KEY": SECRET}}, ("API_KEY",)),
        ({"runtime": {"Password": SECRET}}, ("Password",)),
        ({"endpoints": [{"url": "https://example.invalid", "api_key": SECRET}]}, ("endpoints", "api_key")),
        ({"workers": [{"name": "primary", "credential": SECRET}]}, ("workers", "credential")),
        ({"rows": [[{"token": SECRET}]]}, ("rows", "token")),
        ({"limits": {"max_tokens": SECRET}}, ("max_tokens",)),
        ({"limits": {"token_limit": SECRET}}, ("token_limit",)),
    ],
)
def test_nested_secret_keys_rejected_without_echoing_value(config, path_bits):
    """Reject the existing secret pattern at every nested depth without echoing values."""
    with pytest.raises(ValueError) as exc:
        validate_provider_config(config)
    message = _message(exc.value)
    for bit in path_bits:
        assert bit in message


def test_top_level_secret_error_names_key_and_hides_value():
    """Keep the top-level rejection and name that key path too."""
    with pytest.raises(ValueError) as exc:
        validate_provider_config({"api_key": SECRET})
    message = _message(exc.value)
    assert "api_key" in message


def test_nested_non_secret_config_is_preserved():
    """Keep legitimate nested settings, including names that do not match the pattern."""
    payload = {
        "batch_size": 4,
        "runtime": {
            "max_outputs": 8,
            "labels": ["person", "vehicle"],
            "threshold": 0.5,
            "enabled": True,
            "note": None,
            "workers": [{"name": "primary", "batch_size": 2}],
        },
    }
    assert validate_provider_config(payload) == payload


def test_shared_child_object_is_not_a_cycle():
    """Allow a directed acyclic config that reuses one child mapping."""
    shared = {"batch_size": 1}
    assert validate_provider_config({"left": shared, "right": shared}) == {
        "left": {"batch_size": 1},
        "right": {"batch_size": 1},
    }


def test_moderate_nesting_remains_accepted():
    """Allow ordinary provider nesting below the depth bound."""
    payload = {"runtime": {"detector": {"input": {"width": 640}}}}
    assert validate_provider_config(payload) == payload


def test_excessive_nesting_is_rejected():
    """Reject a deep chain that stays under the old repr size check."""
    node: dict = {"batch_size": 1}
    for _ in range(32):
        node = {"child": node}
    with pytest.raises(ValueError) as exc:
        validate_provider_config(node)
    message = _message(exc.value)
    assert "depth" in message


def test_cycle_is_rejected():
    """Reject a circular provider config instead of walking it forever."""
    node = {"batch_size": 1}
    node["loop"] = node
    with pytest.raises(ValueError) as exc:
        validate_provider_config(node)
    message = _message(exc.value)
    assert "cycle" in message


@pytest.mark.parametrize(
    "value",
    [
        {"labels": {"person", "vehicle"}},
        {"bbox": (0.1, 0.2)},
        {"payload": b"synthetic-bytes"},
        {"score": float("nan")},
        {"score": float("inf")},
        {"worker": object()},
    ],
)
def test_non_json_values_are_rejected(value):
    """Reject types JSON cannot represent, including values with a short repr."""
    with pytest.raises(ValueError) as exc:
        validate_provider_config(value)
    assert SECRET not in str(exc.value)


def test_container_size_uses_json_not_repr():
    """Reject a nested object whose compact JSON exceeds the size cap while repr does not."""
    nested = {f"k{i}": '"' * 80 for i in range(25)}
    assert len(repr(nested)) <= 4096
    assert len(json.dumps(nested, separators=(",", ":"), ensure_ascii=False)) > 4096
    with pytest.raises(ValueError) as exc:
        validate_provider_config({"outer": nested})
    message = _message(exc.value)
    assert "too large" in message
    assert '"' * 80 not in message


def test_total_json_size_bounds_top_level_strings():
    """Reject a wide string map that the per-value repr check never inspected."""
    payload = {f"n{index:02d}": "x" * 1024 for index in range(64)}
    with pytest.raises(ValueError) as exc:
        validate_provider_config(payload)
    message = _message(exc.value)
    assert "size" in message
    assert "x" * 64 not in message


def test_huge_integer_is_rejected_by_json_size():
    """Reject an integer the old dict/list repr check ignored."""
    with pytest.raises(ValueError) as exc:
        validate_provider_config({"count": 10**70000})
    message = _message(exc.value)
    assert "size" in message or "too large" in message
    assert "70000" not in message


def test_overlong_key_is_rejected_without_echoing_it():
    """Reject a key longer than the historical 128-character store limit."""
    key = "batch_" + ("k" * 200)
    with pytest.raises(ValueError) as exc:
        validate_provider_config({key: 1})
    message = _message(exc.value)
    assert key not in message


def test_policy_read_list_redacts_legacy_nested_secrets():
    """Drop secret-like keys from every row a list response would serialize."""
    listed = [policy_read(_policy_row(_legacy_config())).model_dump(mode="json") for _ in range(2)]
    rendered = json.dumps(listed)
    assert SECRET not in rendered
    assert listed[0]["provider_config"]["batch_size"] == 4
    assert listed[0]["provider_config"]["runtime"] == {"endpoint": "https://example.invalid/infer"}
    assert listed[0]["provider_config"]["workers"] == [{"name": "primary"}]
    assert listed[0]["provider_config"]["extra"] == [{"nested": [{}]}]
    assert listed[0] == listed[1]


def _principal() -> Principal:
    return Principal("synthetic-actor", frozenset({"admin"}), "tenant-a", frozenset({"site-a"}))


def _camera() -> CameraEntity:
    return CameraEntity(
        id=CAMERA_ID,
        tenant_id="tenant-a",
        site_id="site-a",
        name="Gate",
        host="192.0.2.20",
        main_path="/main",
        stream_key="gate-fix-020",
    )


@asynccontextmanager
async def _api():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)

    async def identity():
        return _principal()

    async def session_dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_principal] = identity
    app.dependency_overrides[get_session] = session_dependency
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(_camera())
            await session.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        await engine.dispose()


def _policy_body(provider_config: dict) -> dict:
    return {
        "enabled": False,
        "source_mode": "inference",
        "analytics": [],
        "provider_config": provider_config,
    }


def test_put_rejects_nested_secret_with_4xx_and_stores_nothing():
    """The policy route returns 4xx and does not persist a nested plaintext secret."""

    async def scenario():
        async with _api() as (client, sessions):
            response = await client.put(
                f"/api/v1/ai/cameras/{CAMERA_ID}/policy",
                json=_policy_body({"endpoints": [{"api_key": SECRET}]}),
            )
            assert 400 <= response.status_code < 500
            assert SECRET not in response.text
            body = response.json()
            detail = body["detail"] if isinstance(body["detail"], str) else json.dumps(body["detail"])
            assert "api_key" in detail
            assert "endpoints" in detail
            assert SECRET not in detail
            async with sessions() as session:
                row = (
                    await session.execute(
                        select(CameraAIPolicyEntity).where(CameraAIPolicyEntity.camera_id == CAMERA_ID)
                    )
                ).scalar_one_or_none()
                assert row is None

    asyncio.run(scenario())


def test_get_redacts_legacy_nested_secrets():
    """GET readback omits secret-like keys already stored before the write check."""

    async def scenario():
        async with _api() as (client, sessions):
            async with sessions() as session:
                session.add(CameraAIPolicyEntity(camera_id=CAMERA_ID, provider_config_json=_legacy_config()))
                await session.commit()
            response = await client.get(f"/api/v1/ai/cameras/{CAMERA_ID}/policy")
            assert response.status_code == 200
            assert SECRET not in response.text
            config = response.json()["provider_config"]
            assert config["batch_size"] == 4
            assert config["runtime"] == {"endpoint": "https://example.invalid/infer"}
            assert "api_key" not in config["runtime"]
            assert config["workers"] == [{"name": "primary"}]
            assert config["extra"] == [{"nested": [{}]}]

    asyncio.run(scenario())


def test_put_readback_keeps_nested_non_secret_config():
    """A nested config that passes validation is returned by the write response and GET."""

    async def scenario():
        config = {"runtime": {"batch_size": 4, "labels": ["person"]}}
        async with _api() as (client, _sessions):
            created = await client.put(
                f"/api/v1/ai/cameras/{CAMERA_ID}/policy",
                json=_policy_body(config),
            )
            assert created.status_code == 200
            assert created.json()["provider_config"] == config
            fetched = await client.get(f"/api/v1/ai/cameras/{CAMERA_ID}/policy")
            assert fetched.status_code == 200
            assert fetched.json()["provider_config"] == config

    asyncio.run(scenario())
