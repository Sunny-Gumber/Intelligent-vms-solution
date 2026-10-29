"""Native-platform fence-state persistence must flush the rename before revoke ACK."""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_fencing_phase7_step1cb import make_agent_settings, node_agent
from tests.time_control import FIXED_NOW, FrozenDateTime


def bare_agent(tmp_path):
    """Construct only persistence state, without creating an HTTP client."""
    agent = object.__new__(node_agent.NodeAgent)
    agent.settings = SimpleNamespace(fence_state_path=str(tmp_path / "fence.json"))
    agent._fence_state = {"clock_offset_seconds": 0, "authority_mode": "fenced_degraded",
                          "assignments": {"camera:media": {"generation": 8, "fenced": True}}}
    return agent


def test_native_directory_flush_persists_fence_across_restart(tmp_path):
    """The real OS helper succeeds on Windows as well as POSIX, without mocks."""
    first = bare_agent(tmp_path)
    first._save_fence_state()
    second = bare_agent(tmp_path)
    second._fence_state = {}
    second._load_fence_state()
    assert second._fence_state == first._fence_state
    assert not Path(first.settings.fence_state_path + ".tmp").exists()


def test_file_flush_precedes_replace_and_directory_flush(tmp_path, monkeypatch):
    agent = bare_agent(tmp_path)
    events = []
    real_fsync = node_agent.os.fsync

    def file_flush(descriptor):
        events.append("file")
        real_fsync(descriptor)

    def directory_flush(directory):
        assert directory == tmp_path
        assert json.loads(Path(agent.settings.fence_state_path).read_text()) == agent._fence_state
        assert not Path(agent.settings.fence_state_path + ".tmp").exists()
        events.append("directory")

    monkeypatch.setattr(node_agent.os, "fsync", file_flush)
    monkeypatch.setattr(node_agent._ATOMIC_PUBLISH, "fsync_directory", directory_flush)
    agent._save_fence_state()
    assert events == ["file", "directory"]


def test_windows_dispatch_uses_backup_handle_flush(tmp_path, monkeypatch):
    helper = node_agent._ATOMIC_PUBLISH
    calls = []
    monkeypatch.setattr(helper, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(helper, "_fsync_directory_windows", calls.append)
    helper.fsync_directory(tmp_path)
    assert calls == [tmp_path]


def test_node_image_layout_loads_shared_flush_helper(tmp_path, monkeypatch):
    """A standalone node image can load the copied helper beside main.py."""
    copied = tmp_path / "phase8_atomic_publish.py"
    shutil.copyfile(node_agent._ATOMIC_PUBLISH.__file__, copied)
    monkeypatch.setattr(node_agent, "__file__", str(tmp_path / "main.py"))
    loaded = node_agent._load_atomic_publish()
    assert Path(loaded.__file__) == copied
    loaded.fsync_directory(tmp_path)


def test_directory_flush_failure_never_acknowledges_revocation(tmp_path, monkeypatch):
    monkeypatch.setattr(node_agent, "datetime", FrozenDateTime)
    agent = node_agent.NodeAgent(make_agent_settings(tmp_path, RECORDING_MOUNT_PATH=str(tmp_path)))
    acknowledgements = []

    async def deleted(*args):
        return True

    async def ack(identifier):
        acknowledgements.append(identifier)
        return True

    def flush_failure(directory):
        raise OSError("synthetic directory flush failure")

    monkeypatch.setattr(agent, "_delete_execution_key", deleted)
    monkeypatch.setattr(agent, "_ack_revocation", ack)
    monkeypatch.setattr(node_agent._ATOMIC_PUBLISH, "fsync_directory", flush_failure)
    snapshot = {"server_time": FIXED_NOW.isoformat(), "node_id": "node-a", "assignments": [],
                "revocations": [{"revocation_id": "rev-directory", "assignment_id": "assignment",
                                 "camera_id": "cam-1", "role": "media", "revoked_generation": 2,
                                 "execution_key": "gate-live", "created_at": FIXED_NOW.isoformat()}]}
    try:
        with pytest.raises(OSError, match="directory flush"):
            asyncio.run(agent._apply_fence_snapshot(snapshot))
        assert acknowledgements == []
    finally:
        asyncio.run(agent.close())


def test_file_flush_failure_preserves_previous_fence_file(tmp_path, monkeypatch):
    agent = bare_agent(tmp_path)
    path = Path(agent.settings.fence_state_path)
    path.write_text("previous durable state")

    def failure(descriptor):
        raise OSError("synthetic file flush failure")

    monkeypatch.setattr(node_agent.os, "fsync", failure)
    with pytest.raises(OSError, match="file flush"):
        agent._save_fence_state()
    assert path.read_text() == "previous durable state"
