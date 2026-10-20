"""Shared test fixtures.

Design notes:
  * The schema is created by the REAL Alembic migration (once per session into
    a template file that each test copies), so the shipped migration is
    exercised by every test run rather than metadata.create_all.
  * The media gateway is a NullGatewayClient: tests never require MediaMTX.
  * FFmpeg IS used where available, so recording/export tests produce genuine
    media files rather than mocks. Tests needing it are skipped with a visible
    reason when ffmpeg is absent.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "control-api"))
sys.path.insert(0, str(ROOT / "services" / "recording-service"))
sys.path.insert(0, str(ROOT / "packages" / "shared-contracts"))

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG,
                                     reason="ffmpeg/ffprobe not available on PATH")


@contextlib.asynccontextmanager
async def _noop_lifespan(app):  # noqa: ANN001
    """Tests drive the app without background loops."""
    yield


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "vms"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def settings(data_root: Path, monkeypatch):
    from app.config import get_settings, reset_settings_cache

    db_path = data_root / "test.db"
    monkeypatch.setenv("VMS_DATA_ROOT", str(data_root))
    monkeypatch.setenv("VMS_DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("VMS_SECRET_KEY",
                       "test-secret-key-that-is-long-enough-1234567890")
    monkeypatch.setenv("VMS_ENVIRONMENT", "development")
    monkeypatch.setenv("VMS_SEGMENT_SECONDS", "2")
    monkeypatch.setenv("VMS_BOOTSTRAP_ADMIN_PASSWORD", "AdminPassw0rd!Test")
    # Argon2 at production cost (64 MiB x3) makes the suite ~10x slower without
    # testing anything extra. Production defaults are asserted separately in
    # tests/unit/test_security_defaults.py.
    monkeypatch.setenv("VMS_PASSWORD_HASH_MEMORY_KIB", "8192")
    monkeypatch.setenv("VMS_PASSWORD_HASH_TIME_COST", "1")
    # Argon2 at production cost (64 MiB x 3 passes) makes a few hundred test
    # authentications take minutes. CI uses the cheapest safe setting; the
    # production floor is enforced in app/config.py and is NOT relaxed here.
    monkeypatch.setenv("VMS_PASSWORD_HASH_TIME_COST", "1")
    monkeypatch.setenv("VMS_PASSWORD_HASH_MEMORY_KIB", "8192")
    monkeypatch.setenv("VMS_PASSWORD_HASH_PARALLELISM", "1")
    reset_settings_cache()
    resolved = get_settings()
    resolved.ensure_directories()
    yield resolved
    reset_settings_cache()


@pytest.fixture(scope="session")
def migration_template(tmp_path_factory) -> Path:
    """Run the REAL Alembic migration once per session into a template file."""
    root = tmp_path_factory.mktemp("migration-template")
    template = root / "template.db"
    env = dict(os.environ)
    env["VMS_DATABASE_URL"] = f"sqlite+pysqlite:///{template}"
    env["VMS_DATA_ROOT"] = str(root)
    env["VMS_SECRET_KEY"] = "template-secret-key-long-enough-0123456789"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c",
         str(ROOT / "database" / "alembic.ini"), "upgrade", "head"],
        capture_output=True, text=True, env=env, cwd=str(ROOT / "database"))
    assert result.returncode == 0, f"alembic failed:\n{result.stdout}\n{result.stderr}"
    assert template.is_file()
    return template


@pytest.fixture
def migrated_db(settings, migration_template):
    from app.db import session as db

    db.dispose_engine()
    target = Path(settings.database_url.split("///", 1)[1])
    shutil.copyfile(migration_template, target)
    engine = db.get_engine(settings)
    yield engine
    db.dispose_engine()


@pytest.fixture
def db_session(migrated_db):
    from app.db.session import get_session_factory

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def container(settings, migrated_db):
    from app.container import build_container
    from app.media.mediamtx_client import NullGatewayClient

    return build_container(settings, gateway=NullGatewayClient())


@pytest.fixture
def seeded(container, settings):
    """Roles, admin user, default rules and a storage volume."""
    from app.db.session import session_scope
    from app.lifecycle import (
        seed_admin,
        seed_default_rules,
        seed_default_storage,
        seed_roles,
    )

    with session_scope() as session:
        seed_roles(session)
    with session_scope() as session:
        seed_admin(session, settings)
        seed_default_rules(session)
        seed_default_storage(session, settings)
    return container


@pytest.fixture
def client(seeded, settings):
    """TestClient without the lifespan (no background loops in tests)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, container=seeded)
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin_token(client, settings) -> str:
    response = client.post("/api/v1/auth/login", json={
        "username": settings.bootstrap_admin_username,
        "password": settings.bootstrap_admin_password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth(admin_token) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """A real 10-second H.264 test clip produced by ffmpeg."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not available")
    output = tmp_path / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=10",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-g", "10", str(output)],
        check=True, capture_output=True)
    return output
