import asyncio

from app.core.config import settings
from app.core.security import encrypt_secret
from app.models.entities import CameraEntity
import app.services.reconciler as reconciler


class FakeMedia:
    def __init__(self):
        self.added = []

    async def add_or_replace_path(self, stream_key, source):
        self.added.append((stream_key, source))


def camera(key: str, enabled: bool = True):
    return CameraEntity(
        id=key,
        tenant_id="t",
        site_id="s",
        name=key,
        host="10.1.2.3",
        rtsp_port=554,
        main_path="/main",
        sub_path="/sub",
        username_enc=encrypt_secret("admin"),
        password_enc=encrypt_secret("p@ss word"),
        stream_key=key,
        media_node_id="media-local-01",
        enabled=enabled,
        desired_state="provisioned",
    )


def test_source_rebuild_prefers_substream_and_encodes_credentials():
    source = reconciler.source_for_camera(camera("cam-1"))
    assert source == "rtsp://admin:p%40ss%20word@10.1.2.3:554/sub"


def test_reconcile_adds_only_missing_enabled_paths(monkeypatch):
    fake = FakeMedia()
    monkeypatch.setattr(reconciler, "mediamtx", fake)
    old_limit = settings.media_reconcile_max_changes_per_run
    settings.media_reconcile_max_changes_per_run = 10
    try:
        changed, failed = asyncio.run(
            reconciler.reconcile_batch(
                [camera("present"), camera("missing"), camera("disabled", enabled=False)],
                {"present", "present-main"},
            )
        )
    finally:
        settings.media_reconcile_max_changes_per_run = old_limit
    assert changed == 2
    assert failed == 0
    assert [key for key, _source in fake.added] == ["missing", "missing-main"]


def test_reconcile_respects_change_budget(monkeypatch):
    fake = FakeMedia()
    monkeypatch.setattr(reconciler, "mediamtx", fake)
    old_limit = settings.media_reconcile_max_changes_per_run
    settings.media_reconcile_max_changes_per_run = 1
    try:
        changed, failed = asyncio.run(
            reconciler.reconcile_batch([camera("a"), camera("b")], set())
        )
    finally:
        settings.media_reconcile_max_changes_per_run = old_limit
    assert changed == 1
    assert failed == 0
    assert len(fake.added) == 1
