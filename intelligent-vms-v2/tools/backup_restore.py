#!/usr/bin/env python3
"""Configuration backup and restore.

What a backup contains by default:
    manifest.json     product version, schema revision, timestamp, contents
    database.sql      pg_dump (PostgreSQL) or a consistent SQLite copy
    config/           the TOML configuration files
    inventory.json    camera/site/user inventory for human inspection

What it does NOT contain by default:
    the master key    (--include-keys is required, and warns loudly)
    recordings        (they are far too large; back those up with your
                       storage tooling - see docs/operations/)
    exports           (reproducible from recordings)

The master-key exclusion is deliberate and is the single most important thing
to understand about restores: WITHOUT THE MASTER KEY, RESTORED CAMERA
CREDENTIALS CANNOT BE DECRYPTED and every camera must be re-authenticated.
`restore` detects this and tells you plainly rather than failing later with a
confusing decryption error.

Usage:
    python tools/backup_restore.py backup  --output /backups
    python tools/backup_restore.py backup  --output /backups --include-keys
    python tools/backup_restore.py restore --archive /backups/vms-backup-....zip
    python tools/backup_restore.py inspect --archive /backups/vms-backup-....zip
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "control-api"))
sys.path.insert(0, str(ROOT / "packages" / "shared-contracts"))

from app.config import Settings, get_settings
from app.version import REQUIRED_SCHEMA_REVISION, VERSION

MANIFEST_NAME = "manifest.json"
KEY_WARNING = (
    "This archive contains the MASTER ENCRYPTION KEY. Anyone holding it can "
    "decrypt every stored camera credential. Store it with the same controls "
    "you apply to the cameras themselves."
)


def _sqlite_path(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite"):
        return None
    return Path(database_url.split("///", 1)[-1])


def dump_database(settings: Settings, destination: Path) -> dict:
    """Dump the database. Returns a manifest fragment describing what happened."""
    sqlite_file = _sqlite_path(settings.database_url)

    if sqlite_file is not None:
        if not sqlite_file.is_file():
            return {"kind": "sqlite", "ok": False, "detail": "database file not found"}
        # sqlite3 backup API gives a consistent snapshot even while in use.
        with sqlite3.connect(str(sqlite_file)) as source, \
                sqlite3.connect(str(destination)) as target:
            source.backup(target)
        return {"kind": "sqlite", "ok": True, "bytes": destination.stat().st_size}

    if shutil.which("pg_dump") is None:
        return {"kind": "postgresql", "ok": False,
                "detail": "pg_dump is not on PATH; install postgresql-client"}

    result = subprocess.run(
        ["pg_dump", "--no-owner", "--no-privileges", "--format=plain",
         "--file", str(destination), settings.database_url],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {"kind": "postgresql", "ok": False,
                "detail": result.stderr.strip()[:400]}
    return {"kind": "postgresql", "ok": True, "bytes": destination.stat().st_size}


def collect_inventory(settings: Settings) -> dict:
    """A human-readable summary so an operator can verify a backup at a glance."""
    from app.db import session as db
    from app.db.models import Camera, Role, Site, StorageVolume, User
    from sqlalchemy import func, select

    db.get_engine(settings)
    with db.session_scope() as session:
        def count(model):
            return int(session.execute(select(func.count()).select_from(model)).scalar_one())

        cameras = session.execute(select(Camera)).scalars().all()
        return {
            "counts": {
                "cameras": count(Camera), "sites": count(Site), "users": count(User),
                "roles": count(Role), "storage_volumes": count(StorageVolume),
            },
            # Credentials are deliberately absent; only non-secret identity fields.
            "cameras": [
                {"id": c.id, "name": c.name, "host": c.host, "port": c.port,
                 "protocol": c.protocol, "site_id": c.site_id, "state": c.state,
                 "has_stored_credential": bool(c.credential and c.credential.secret_blob)}
                for c in cameras
            ],
        }


def backup(settings: Settings, output_dir: Path, include_keys: bool = False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_path = output_dir / f"vms-backup-{stamp}.zip"

    with tempfile.TemporaryDirectory() as workspace:
        work = Path(workspace)
        database_file = work / "database.sql"
        database_info = dump_database(settings, database_file)

        try:
            inventory = collect_inventory(settings)
        except Exception as exc:  # noqa: BLE001
            inventory = {"error": str(exc)[:300]}

        manifest = {
            "product": "Intelligent VMS",
            "version": VERSION,
            "schema_revision": REQUIRED_SCHEMA_REVISION,
            "created_at": dt.datetime.now(dt.UTC).isoformat(),
            "database": database_info,
            "includes_master_key": include_keys,
            "includes_recordings": False,
            "inventory": inventory,
            "restore_notes": [
                "Recordings are NOT included. Back up storage volumes separately.",
                ("Master key NOT included: restored camera credentials will be "
                 "undecryptable and every camera must be re-authenticated."
                 if not include_keys else KEY_WARNING),
            ],
        }
        (work / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2),
                                          encoding="utf-8")

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(work / MANIFEST_NAME, MANIFEST_NAME)
            if database_file.is_file():
                archive.write(database_file, "database.sql")

            config_dir = ROOT / "config"
            if config_dir.is_dir():
                for path in config_dir.rglob("*"):
                    if path.is_file() and not path.name.startswith("local."):
                        archive.write(path, f"config/{path.relative_to(config_dir)}")

            if include_keys:
                key_file = settings.keys_dir / "vms-master.key"
                if key_file.is_file():
                    archive.write(key_file, "keys/vms-master.key")

    if not sys.platform.startswith("win"):
        os.chmod(archive_path, 0o600)

    print(f"backup written: {archive_path}")
    print(f"  database   : {database_info.get('kind')} "
          f"{'ok' if database_info.get('ok') else 'FAILED: ' + str(database_info.get('detail'))}")
    print(f"  master key : {'INCLUDED - protect this file' if include_keys else 'excluded'}")
    print("  recordings : excluded by design")
    return archive_path


def inspect(archive_path: Path) -> dict:
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME))
        names = archive.namelist()
    manifest["_contents"] = names
    print(json.dumps(manifest, indent=2))
    return manifest


def restore(settings: Settings, archive_path: Path, *, force: bool = False) -> int:
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME))

        if manifest.get("schema_revision") != REQUIRED_SCHEMA_REVISION and not force:
            print(f"ERROR: archive schema {manifest.get('schema_revision')} does not "
                  f"match this build ({REQUIRED_SCHEMA_REVISION}).", file=sys.stderr)
            print("Upgrade to the matching version first, or pass --force to restore "
                  "anyway and run migrations afterwards.", file=sys.stderr)
            return 2

        sqlite_file = _sqlite_path(settings.database_url)
        if sqlite_file is not None:
            if sqlite_file.exists():
                backup_copy = sqlite_file.with_suffix(".pre-restore")
                shutil.copyfile(sqlite_file, backup_copy)
                print(f"existing database copied to {backup_copy}")
            sqlite_file.parent.mkdir(parents=True, exist_ok=True)
            with archive.open("database.sql") as source, open(sqlite_file, "wb") as target:
                shutil.copyfileobj(source, target)
            print(f"database restored to {sqlite_file}")
        else:
            temporary = Path(tempfile.gettempdir()) / "vms-restore.sql"
            with archive.open("database.sql") as source, open(temporary, "wb") as target:
                shutil.copyfileobj(source, target)
            print(f"PostgreSQL dump extracted to {temporary}")
            print(f"Run:  psql \"{settings.database_url}\" -f {temporary}")

        if "keys/vms-master.key" in archive.namelist():
            settings.keys_dir.mkdir(parents=True, exist_ok=True)
            key_path = settings.keys_dir / "vms-master.key"
            with archive.open("keys/vms-master.key") as source, open(key_path, "wb") as target:
                shutil.copyfileobj(source, target)
            if not sys.platform.startswith("win"):
                os.chmod(key_path, 0o600)
            print(f"master key restored to {key_path}")
        else:
            print()
            print("WARNING: this archive contains no master key.")
            print("         Stored camera credentials CANNOT be decrypted.")
            print("         Re-enter the password for every camera after starting up.")

    print()
    print("Next: run database migrations, then start the services.")
    print("  cd database && alembic upgrade head")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    backup_parser = sub.add_parser("backup", help="create a configuration backup")
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument("--include-keys", action="store_true",
                               help="include the master encryption key (handle with care)")

    restore_parser = sub.add_parser("restore", help="restore from an archive")
    restore_parser.add_argument("--archive", type=Path, required=True)
    restore_parser.add_argument("--force", action="store_true",
                                help="restore despite a schema revision mismatch")

    inspect_parser = sub.add_parser("inspect", help="print an archive manifest")
    inspect_parser.add_argument("--archive", type=Path, required=True)

    args = parser.parse_args()
    settings = get_settings()

    if args.command == "backup":
        backup(settings, args.output, args.include_keys)
        return 0
    if args.command == "inspect":
        inspect(args.archive)
        return 0
    return restore(settings, args.archive, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
