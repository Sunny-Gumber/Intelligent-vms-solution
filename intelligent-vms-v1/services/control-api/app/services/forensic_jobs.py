"""Opt-in, single-process forensic job admission and isolated Docker execution.

All retained jobs count against admission. Restart fails closed on a nonempty
spool: this experimental local executor does not claim distributed durability.
The spool and Docker daemon must be dedicated to forensic processing.
"""

import asyncio
import json
import hashlib
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

MAX_JOBS = 4
MAX_TENANT_JOBS = 2
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_DERIVATIVE_BYTES = 128 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024
PROCESS_SECONDS = 300
TERMINAL = frozenset({"completed", "failed", "cancelled"})
IMAGE_RE = re.compile(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}\Z")
log = logging.getLogger("vms.security.audit")
_CONTAINER_WRAPPER = (
    "import pathlib,subprocess,sys,time;"
    "result=subprocess.run([sys.executable,'/app/enhance.py',*sys.argv[1:]],check=False);"
    "pathlib.Path('/job/completed' if result.returncode==0 else '/job/failed').touch();"
    "time.sleep(3600)"
)


@dataclass
class ForensicJob:
    """Server-owned identity, immutable request binding, and bounded job state."""

    id: str
    camera_id: str
    tenant_id: str
    site_id: str
    subject: str
    start: str
    duration: float
    settings: dict
    directory: Path
    state: str = "staging"
    task: asyncio.Task | None = None
    events: list[dict] = field(default_factory=list)
    cleanup_confirmed: bool = True
    cancel_requested: bool = False
    worker_image: str = ""

    def snapshot(self) -> dict:
        """Return a path-free report containing request binding and audit events."""
        return {
            "id": self.id, "camera_id": self.camera_id, "state": self.state,
            "start": self.start, "duration": self.duration, "settings": self.settings,
            "worker_image": self.worker_image,
            "audit": list(self.events),
            "disclaimer": "Enhanced interpretation; original recordings are unchanged.",
        }


class ForensicJobs:
    """Bound total retained work and run at most one isolated decoder at a time."""

    def __init__(self) -> None:
        """Construct an idle registry; no process or filesystem work starts here."""
        self.jobs: dict[str, ForensicJob] = {}
        self.worker_slot = asyncio.Semaphore(1)
        self.root: Path | None = None
        self.unhealthy = False

    def prepare(self, root: str, image: str) -> None:
        """Validate pinned image and an administrator-provisioned empty spool.

        Raises HTTPException(503) for unsafe configuration or restart leftovers.
        """
        if self.unhealthy or not root or not IMAGE_RE.fullmatch(image):
            raise HTTPException(503, "Isolated forensic executor is not configured")
        candidate = Path(root)
        if (not candidate.is_absolute() or candidate.is_symlink() or candidate.is_junction()
                or not candidate.is_dir() or "," in str(candidate)):
            raise HTTPException(503, "Forensic spool must be an existing private directory")
        if self.root is None:
            if next(candidate.iterdir(), None) is not None:
                raise HTTPException(503, "Forensic spool recovery requires administrator review")
            try:
                (candidate / ".executor-lock").mkdir(mode=0o700)
            except FileExistsError as exc:
                raise HTTPException(503, "Forensic spool already has an executor") from exc
            self.root = candidate.resolve()
        elif candidate.resolve() != self.root:
            raise HTTPException(503, "Forensic executor configuration changed")

    def reserve(self, camera, principal, start: str, duration: float, options: dict) -> ForensicJob:
        """Reserve bounded storage before opening any authorized recording export."""
        if self.root is None:
            raise HTTPException(503, "Forensic executor is unavailable")
        tenant_jobs = sum(job.tenant_id == camera.tenant_id for job in self.jobs.values())
        own_jobs = sum(job.subject == principal.subject for job in self.jobs.values())
        if len(self.jobs) >= MAX_JOBS or tenant_jobs >= MAX_TENANT_JOBS or own_jobs >= 1:
            raise HTTPException(429, "Forensic job quota reached; delete a retained job first")
        identifier = uuid4().hex
        directory = self.root / identifier
        directory.mkdir(mode=0o700)
        job = ForensicJob(identifier, camera.id, camera.tenant_id, camera.site_id,
                          principal.subject, start, duration, dict(options), directory)
        self.jobs[identifier] = job
        self.transition(job, "staging")
        return job

    def transition(self, job: ForensicJob, state: str) -> None:
        """Persist a bounded audit event before exposing a new processing state."""
        event = {"state": state, "at": datetime.now(timezone.utc).isoformat(),
                 "subject_hash": hashlib.sha256(job.subject.encode()).hexdigest()[:16],
                 "tenant_id": job.tenant_id, "site_id": job.site_id}
        # There are at most staging/queued/running/terminal transitions per job.
        if len(job.events) >= 8:
            raise RuntimeError("Forensic transition bound exceeded")
        with (job.directory / "audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        job.events.append(event)
        job.state = state
        log.info("forensic_audit %s", json.dumps({
            "job_id": job.id, "state": state, "at": event["at"],
            "subject_hash": hashlib.sha256(job.subject.encode()).hexdigest()[:16],
            "tenant_id": job.tenant_id[:128], "site_id": str(job.site_id)[:128],
        }, separators=(",", ":")))

    async def command(self, *args: str, timeout: float = PROCESS_SECONDS, check: bool = True) -> int:
        """Run Docker without shell/output capture, killing the CLI on interruption."""
        process = await asyncio.create_subprocess_exec(
            "docker", *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            result = await asyncio.wait_for(process.wait(), timeout)
            if result and check:
                raise RuntimeError("Isolated forensic command failed")
            return result
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def arguments(self, job: ForensicJob, image: str) -> list[str]:
        """Build fixed Linux-container resource controls and numeric-only CLI options."""
        arguments = [
            "run", "--detach", "--name", "vms-forensic-" + job.id, "--pull=never",
            "--network=none", "--cpus=1", "--memory=512m", "--memory-swap=512m",
            "--pids-limit=64", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65534:65534",
            "--log-driver=none", "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            "--tmpfs=/job:rw,noexec,nosuid,size=128m,mode=1777",
            "--mount", f"type=bind,source={job.directory / 'original.mp4'},target=/source.mp4,readonly",
            "--entrypoint=python", image, "-c", _CONTAINER_WRAPPER,
            "--input", "/source.mp4", "--output", "/job/enhanced.mp4",
        ]
        for key, value in job.settings.items():
            if key == "roi" and value is not None:
                arguments.extend(["--roi", ",".join(str(number) for number in value)])
            elif key != "roi":
                arguments.extend(["--" + key.replace("_", "-"), str(value)])
        return arguments

    async def execute(self, job: ForensicJob, image: str) -> None:
        """Execute queued work, copy bounded artifacts, and always remove its container."""
        container = "vms-forensic-" + job.id
        state = "failed"
        started = False
        acquired = False
        try:
            await self.worker_slot.acquire()
            acquired = True
            if self.unhealthy:
                raise RuntimeError("Executor cleanup is uncertain")
            if acquired:
                job.worker_image = image
                self.transition(job, "running")
                started = True
                job.cleanup_confirmed = False
                await self.command(*self.arguments(job, image), timeout=30)
                async with asyncio.timeout(PROCESS_SECONDS):
                    while await self.command("exec", container, "test", "-f", "/job/completed",
                                            timeout=10, check=False):
                        if not await self.command("exec", container, "test", "-f", "/job/failed",
                                                  timeout=10, check=False):
                            raise RuntimeError("Isolated worker rejected the media")
                        await asyncio.sleep(0.5)
                for name, maximum in (("enhanced.mp4", MAX_DERIVATIVE_BYTES),
                                      ("enhanced.mp4.forensic.json", MAX_REPORT_BYTES)):
                    target = job.directory / name
                    await self.command("cp", container + ":/job/" + name, str(target), timeout=30)
                    metadata = target.lstat()
                    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
                        raise RuntimeError("Invalid forensic artifact")
                await self.verify_report(job)
                state = "completed"
        except asyncio.CancelledError:
            state = "cancelled"
        except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
            state = "failed"
        finally:
            if started:
                try:
                    await self.command("rm", "--force", container, timeout=30)
                    job.cleanup_confirmed = True
                except (OSError, RuntimeError, TimeoutError):
                    # Never claim successful cancellation or release storage on uncertain cleanup.
                    state = "failed"
                    self.unhealthy = True
            try:
                self.transition(job, state)
            finally:
                if acquired:
                    self.worker_slot.release()

    async def verify_report(self, job: ForensicJob) -> None:
        """Reject provenance that does not hash the exact staged source and derivative."""
        report = json.loads((job.directory / "enhanced.mp4.forensic.json").read_text(encoding="utf-8"))
        for filename, key in (("original.mp4", "source_sha256"), ("enhanced.mp4", "derivative_sha256")):
            digest = hashlib.sha256()
            with (job.directory / filename).open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    # Cooperatively bound API scheduling delay and make cancellation
                    # close the file before any terminal state/deletion is exposed.
                    await asyncio.sleep(0)
            if not isinstance(report, dict) or report.get(key) != digest.hexdigest():
                raise RuntimeError("Forensic provenance hash mismatch")

    async def cancel(self, job: ForensicJob) -> None:
        """Wait for queued/running cancellation and confirmed container cleanup."""
        if job.state in TERMINAL:
            return
        if job.task is not None:
            if not job.cancel_requested:
                job.cancel_requested = True
                job.task.cancel()
            try:
                await asyncio.shield(job.task)
            except asyncio.CancelledError:
                # A task cancelled before its first scheduling never enters execute().
                if not job.task.done():
                    raise
                if job.state not in TERMINAL:
                    self.transition(job, "cancelled")

    async def shutdown(self) -> None:
        """Cancel bounded active jobs during API shutdown; retained audit remains on disk."""
        await asyncio.gather(*(self.cancel(job) for job in self.jobs.values()))

    def delete(self, job: ForensicJob) -> None:
        """Delete a terminal job and its sensitive derivatives after authorized request."""
        if job.state not in TERMINAL:
            raise HTTPException(409, "Cancel the forensic job before deleting it")
        if not job.cleanup_confirmed:
            raise HTTPException(409, "Executor cleanup requires administrator review")
        log.info("forensic_audit %s", json.dumps({"job_id": job.id, "state": "deleted",
                                                "subject_hash": hashlib.sha256(job.subject.encode()).hexdigest()[:16]}))
        shutil.rmtree(job.directory)
        del self.jobs[job.id]


forensic_jobs = ForensicJobs()
