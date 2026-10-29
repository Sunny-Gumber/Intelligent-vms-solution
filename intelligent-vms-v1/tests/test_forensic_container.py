"""Opt-in real Linux Docker job qualification; no cameras or production services.

Build services/forensic_worker with Docker and set VMS_FORENSIC_TEST_IMAGE to
its immutable sha256 image ID/digest. The CI gate supplies the freshly built ID.
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.services.forensic_jobs import ForensicJobs

IMAGE = os.environ.get("VMS_FORENSIC_TEST_IMAGE", "")
pytestmark = pytest.mark.skipif(
    not IMAGE or not shutil.which("docker") or not shutil.which("ffmpeg"),
    reason="Real Docker gate requires VMS_FORENSIC_TEST_IMAGE, Docker and FFmpeg",
)


def test_real_container_resource_controls_hashes_and_cancellation(tmp_path):
    """Process synthetic media, inspect enforced limits, then cancel and confirm removal."""
    seed = tmp_path / "seed.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=gray:s=64x48:r=10", "-t", "0.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(seed),
    ], check=True, timeout=30)
    original = seed.read_bytes()
    spool = tmp_path / "jobs"
    spool.mkdir()

    async def scenario():
        service = ForensicJobs()
        service.prepare(str(spool.resolve()), IMAGE)
        camera = SimpleNamespace(id="synthetic", tenant_id="test-tenant", site_id="test-site")
        owner = SimpleNamespace(subject="test-owner")
        job = service.reserve(camera, owner, "2026-01-01T00:00:00Z", 0.5,
                              {"gamma": 1.2, "roi": (8, 8, 32, 24)})
        (job.directory / "original.mp4").write_bytes(original)
        actual_command = service.command
        inspected = []

        async def inspect_controls(*args, **kwargs):
            result = await actual_command(*args, **kwargs)
            if args[0] == "run":
                name = "vms-forensic-" + job.id
                inspect = subprocess.run(["docker", "inspect", "--format", "{{json .HostConfig}}", name],
                                         check=True, capture_output=True, text=True, timeout=30)
                config = json.loads(inspect.stdout)
                assert config["NetworkMode"] == "none"
                assert config["NanoCpus"] == 1_000_000_000
                assert config["Memory"] == 512 * 1024 * 1024
                assert config["MemorySwap"] == 512 * 1024 * 1024
                assert config["PidsLimit"] == 64
                assert config["ReadonlyRootfs"] is True
                assert "ALL" in config["CapDrop"]
                assert any("no-new-privileges" in value for value in config["SecurityOpt"])
                assert "size=128m" in config["Tmpfs"]["/job"]
                inspected.append(True)
            return result

        service.command = inspect_controls
        service.transition(job, "queued")
        await service.execute(job, IMAGE)
        assert job.state == "completed", {"state": job.state, "failure_code": job.failure_code,
                                          "resource_controls_verified": bool(inspected)}
        assert inspected == [True]
        assert job.cleanup_confirmed
        assert (job.directory / "original.mp4").read_bytes() == original
        report = json.loads((job.directory / "enhanced.mp4.forensic.json").read_text())
        assert report["source_sha256"] == hashlib.sha256(original).hexdigest()
        assert report["derivative_sha256"] == hashlib.sha256((job.directory / "enhanced.mp4").read_bytes()).hexdigest()
        assert report["filter_graph"].startswith("crop=32:24:8:8")
        assert report["source_media"]["width"] == 64
        _assert_removed(job.id)
        service.delete(job)

        cancelled = service.reserve(camera, owner, "2026-01-01T00:00:00Z", 0.5, {})
        (cancelled.directory / "original.mp4").write_bytes(original)
        created = asyncio.Event()

        async def pause_after_creation(*args, **kwargs):
            result = await actual_command(*args, **kwargs)
            if args[0] == "run":
                created.set()
                await asyncio.Event().wait()
            return result

        service.command = pause_after_creation
        service.transition(cancelled, "queued")
        cancelled.task = asyncio.create_task(service.execute(cancelled, IMAGE))
        await asyncio.wait_for(created.wait(), 45)
        await service.cancel(cancelled)
        assert cancelled.state == "cancelled"
        assert cancelled.cleanup_confirmed
        _assert_removed(cancelled.id)
        service.delete(cancelled)
        assert seed.read_bytes() == original

    asyncio.run(scenario())


def _assert_removed(identifier):
    """Verify the engine no longer retains the named job after success/cancellation."""
    result = subprocess.run(["docker", "inspect", "vms-forensic-" + identifier],
                            capture_output=True, check=False, timeout=30)
    assert result.returncode != 0
