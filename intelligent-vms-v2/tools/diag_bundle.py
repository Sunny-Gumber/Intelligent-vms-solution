#!/usr/bin/env python3
"""Customer diagnostic / support bundle.

Collects what support actually needs to diagnose a VMS problem, and NOTHING
that would leak the customer's security posture:

  INCLUDED   version and build metadata, effective configuration with secrets
             redacted, component health, storage diagnostics, recent log tail
             (already redacted by the logging pipeline), camera inventory
             without credentials, recent events/alarms summary, recording
             health, OS/platform facts, Python dependency versions.

  EXCLUDED   the master key, camera passwords, API keys, refresh tokens, JWT
             secret, SMTP/OIDC secrets, licence private material, and any
             recorded video or snapshot.

The exclusion is enforced by an explicit redaction pass AND verified by
tests/integration/test_tools.py, which plants known secrets and asserts they
do not appear anywhere in the produced archive.

Usage:
    python tools/diag_bundle.py --output /tmp [--log-lines 2000]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "control-api"))
sys.path.insert(0, str(ROOT / "packages" / "shared-contracts"))

from app.config import Settings, get_settings  # noqa: E402
from app.logging_setup import redact_text  # noqa: E402
from app.version import version_info  # noqa: E402

SECRET_FIELDS = {
    "secret_key", "bootstrap_admin_password", "smtp_password", "smtp_user",
    "oidc_client_secret", "media_gateway_password", "database_url",
}
REDACTED = "***redacted***"


def redact_settings(settings: Settings) -> dict:
    """Effective configuration with every secret removed.

    `database_url` is redacted wholesale because it routinely embeds a
    password; the driver name is kept because support needs it.
    """
    data = json.loads(settings.model_dump_json())
    for field in SECRET_FIELDS:
        if field in data and data[field] not in (None, ""):
            data[field] = REDACTED
    data["database_driver"] = settings.database_url.split("://", 1)[0]
    return data


def platform_facts() -> dict:
    import shutil

    facts = {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": __import__("os").cpu_count(),
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
    }
    try:
        import psutil

        memory = psutil.virtual_memory()
        facts["memory_total_mb"] = round(memory.total / 1e6, 1)
        facts["memory_percent"] = memory.percent
        facts["boot_time"] = dt.datetime.fromtimestamp(
            psutil.boot_time(), tz=dt.timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        pass
    return facts


def dependency_versions() -> dict:
    from importlib import metadata

    wanted = ["fastapi", "SQLAlchemy", "alembic", "pydantic", "httpx", "structlog",
              "cryptography", "argon2-cffi", "PyJWT", "psutil", "uvicorn",
              "prometheus-client", "psycopg", "onnxruntime"]
    out = {}
    for name in wanted:
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = "not installed"
    return out


def database_snapshot(settings: Settings) -> dict:
    from sqlalchemy import func, select

    from app.db import session as db
    from app.db.models import (
        Alarm, AnalyticsEvent, AuditEntry, Camera, Event, ExportJob,
        RecordingSegment, StorageVolume, User, WorkerHeartbeat,
    )
    from app.storage import diagnostics as storage_diagnostics

    db.get_engine(settings)
    snapshot: dict = {}
    with db.session_scope() as session:
        def count(model):  # noqa: ANN001
            return int(session.execute(
                select(func.count()).select_from(model)).scalar_one())

        snapshot["counts"] = {
            "cameras": count(Camera), "users": count(User),
            "segments": count(RecordingSegment), "events": count(Event),
            "alarms": count(Alarm), "analytics_events": count(AnalyticsEvent),
            "exports": count(ExportJob), "audit_entries": count(AuditEntry),
            "storage_volumes": count(StorageVolume),
        }

        snapshot["camera_states"] = dict(session.execute(
            select(Camera.state, func.count()).group_by(Camera.state)).all())
        snapshot["recording_states"] = dict(session.execute(
            select(Camera.recording_state, func.count())
            .group_by(Camera.recording_state)).all())

        # Camera identity only - never credentials.
        snapshot["cameras"] = [
            {"id": c.id, "name": c.name, "host": c.host, "port": c.port,
             "protocol": c.protocol, "vendor": c.vendor, "model": c.model,
             "state": c.state, "recording_state": c.recording_state,
             "state_detail": c.state_detail,
             "capabilities": c.capabilities, "consecutive_failures": c.consecutive_failures}
            for c in session.execute(select(Camera).limit(500)).scalars().all()
        ]

        snapshot["recent_events"] = [
            {"at": e.occurred_at.isoformat(), "type": e.type, "severity": e.severity,
             "camera_id": e.camera_id}
            for e in session.execute(
                select(Event).order_by(Event.occurred_at.desc()).limit(200)
            ).scalars().all()
        ]

        snapshot["open_alarms"] = [
            {"id": a.id, "title": a.title, "severity": a.severity, "state": a.state,
             "first_event_at": a.first_event_at.isoformat(), "count": a.event_count}
            for a in session.execute(
                select(Alarm).where(Alarm.state.in_(("new", "active")))
                .order_by(Alarm.first_event_at.desc()).limit(100)).scalars().all()
        ]

        snapshot["workers"] = [
            {"worker_id": w.worker_id, "kind": w.kind, "state": w.state,
             "heartbeat_at": w.heartbeat_at.isoformat(), "metrics": w.metrics}
            for w in session.execute(select(WorkerHeartbeat)).scalars().all()
        ]

        snapshot["storage"] = [d.__dict__ for d in
                               storage_diagnostics.diagnose_all(session)]
    return snapshot


def tail_logs(settings: Settings, max_lines: int = 2000) -> dict[str, str]:
    """Log tails. The logging pipeline already redacts credentials; we redact again."""
    out: dict[str, str] = {}
    if not settings.logs_dir.is_dir():
        return out
    for path in sorted(settings.logs_dir.glob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            out[path.name] = f"<unreadable: {exc}>"
            continue
        out[path.name] = redact_text("\n".join(lines[-max_lines:]))
    return out


def build(settings: Settings, output_dir: Path, log_lines: int = 2000) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = output_dir / f"vms-diagnostics-{stamp}.zip"

    sections: dict[str, object] = {
        "version": version_info(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "platform": platform_facts(),
        "dependencies": dependency_versions(),
        "configuration": redact_settings(settings),
    }
    try:
        sections["database"] = database_snapshot(settings)
    except Exception as exc:  # noqa: BLE001
        sections["database"] = {"error": str(exc)[:500]}

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("diagnostics.json",
                         json.dumps(sections, indent=2, default=str))
        for name, content in tail_logs(settings, log_lines).items():
            archive.writestr(f"logs/{name}", content)
        archive.writestr("README.txt", (
            "Intelligent VMS diagnostic bundle\n"
            "=================================\n\n"
            "Contains: version, redacted configuration, platform facts, dependency\n"
            "versions, database counts, camera inventory (no credentials), recent\n"
            "events and alarms, storage diagnostics and redacted log tails.\n\n"
            "Deliberately excluded: the master encryption key, camera passwords,\n"
            "API keys, tokens, SMTP/OIDC secrets, recorded video and snapshots.\n"))

    print(f"diagnostic bundle written: {archive_path}")
    print(f"  size: {archive_path.stat().st_size / 1024:.1f} KiB")
    print("  secrets, recordings and snapshots are excluded by design")
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=Path.cwd())
    parser.add_argument("--log-lines", type=int, default=2000)
    args = parser.parse_args()
    build(get_settings(), args.output, args.log_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
