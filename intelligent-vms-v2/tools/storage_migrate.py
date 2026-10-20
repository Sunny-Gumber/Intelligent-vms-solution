#!/usr/bin/env python3
"""Move recordings between storage volumes safely.

The order of operations is the whole point of this tool. Moving evidence is a
one-way mistake if done carelessly, so each segment follows:

    1. copy   source -> destination  (streamed, never loaded into memory)
    2. verify SHA-256 of the copy equals SHA-256 of the source
    3. update the index row to point at the new volume and path
    4. commit the index change
    5. ONLY THEN delete the source file

If anything fails at steps 1-3 the source is untouched and the index still
points at it. If the process is killed between 4 and 5, the result is a
harmless orphan file on the old volume, which `--clean-orphans` can sweep
later. At no point can the index reference a file that does not exist.

Usage:
    python tools/storage_migrate.py --from primary --to archive --dry-run
    python tools/storage_migrate.py --from primary --to archive --older-than-days 30
    python tools/storage_migrate.py --from primary --to archive --camera <id> --limit 500
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "control-api"))
sys.path.insert(0, str(ROOT / "packages" / "shared-contracts"))

from sqlalchemy import select  # noqa: E402

from vms_contracts.enums import SegmentState  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.crypto import sha256_file  # noqa: E402
from app.core.paths import resolve_within  # noqa: E402
from app.core.timeutil import utcnow  # noqa: E402
from app.db import session as db  # noqa: E402
from app.db.models import RecordingSegment, StorageVolume  # noqa: E402


@dataclass
class MigrationResult:
    examined: int = 0
    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_moved: int = 0
    errors: list[str] = field(default_factory=list)

    def report(self) -> str:
        return (f"examined={self.examined} migrated={self.migrated} "
                f"skipped={self.skipped} failed={self.failed} "
                f"moved={self.bytes_moved / 1e6:.1f} MB")


def find_volume(session, name_or_id: str) -> StorageVolume:  # noqa: ANN001
    volume = session.execute(
        select(StorageVolume).where(StorageVolume.name == name_or_id)
    ).scalar_one_or_none()
    if volume is None:
        volume = session.get(StorageVolume, name_or_id)
    if volume is None:
        raise SystemExit(f"storage volume {name_or_id!r} not found")
    return volume


def migrate(session, source: StorageVolume, destination: StorageVolume, *,  # noqa: ANN001
            older_than_days: int | None, camera_id: str | None,
            limit: int, dry_run: bool) -> MigrationResult:
    result = MigrationResult()

    statement = select(RecordingSegment).where(
        RecordingSegment.volume_id == source.id,
        RecordingSegment.state == SegmentState.FINALIZED.value)
    if older_than_days is not None:
        statement = statement.where(
            RecordingSegment.started_at < utcnow() - dt.timedelta(days=older_than_days))
    if camera_id:
        statement = statement.where(RecordingSegment.camera_id == camera_id)
    statement = statement.order_by(RecordingSegment.started_at).limit(limit)

    for segment in session.execute(statement).scalars().all():
        result.examined += 1
        try:
            source_path = resolve_within(Path(source.path), segment.relative_path)
            target_path = resolve_within(Path(destination.path), segment.relative_path)
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.errors.append(f"{segment.id}: path error {exc}")
            continue

        if not source_path.is_file():
            result.skipped += 1
            result.errors.append(f"{segment.id}: source file missing")
            continue

        if dry_run:
            result.migrated += 1
            result.bytes_moved += source_path.stat().st_size
            continue

        try:
            # 1. copy
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = target_path.with_suffix(target_path.suffix + ".migrating")
            shutil.copy2(source_path, temporary)

            # 2. verify before anything irreversible happens
            source_digest, source_size = sha256_file(source_path)
            target_digest, target_size = sha256_file(temporary)
            if source_digest != target_digest or source_size != target_size:
                temporary.unlink(missing_ok=True)
                result.failed += 1
                result.errors.append(f"{segment.id}: checksum mismatch after copy")
                continue
            temporary.replace(target_path)

            # 3-4. repoint the index and commit
            segment.volume_id = destination.id
            segment.checksum = target_digest
            session.commit()

            # 5. only now remove the source
            source_path.unlink(missing_ok=True)

            result.migrated += 1
            result.bytes_moved += target_size
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            result.failed += 1
            result.errors.append(f"{segment.id}: {exc}")

    return result


def clean_orphans(session, volume: StorageVolume, *, dry_run: bool) -> int:  # noqa: ANN001
    """Remove files on a volume that the index no longer references."""
    indexed = {
        row for row in session.execute(
            select(RecordingSegment.relative_path)
            .where(RecordingSegment.volume_id == volume.id)).scalars().all()
    }
    root = Path(volume.path)
    removed = 0
    for path in (root / "recordings").rglob("*.mp4"):
        relative = path.relative_to(root).as_posix()
        if relative in indexed:
            continue
        removed += 1
        if not dry_run:
            path.unlink(missing_ok=True)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="source", required=True,
                        help="source volume name or id")
    parser.add_argument("--to", dest="destination", required=True,
                        help="destination volume name or id")
    parser.add_argument("--older-than-days", type=int, default=None)
    parser.add_argument("--camera", default=None, help="limit to one camera id")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--clean-orphans", action="store_true",
                        help="after migrating, delete unindexed files on the source")
    args = parser.parse_args()

    settings = get_settings()
    db.get_engine(settings)

    with db.session_scope() as session:
        source = find_volume(session, args.source)
        destination = find_volume(session, args.destination)
        if source.id == destination.id:
            raise SystemExit("source and destination are the same volume")

        print(f"migrating from {source.name} ({source.path}) "
              f"to {destination.name} ({destination.path})")
        if args.dry_run:
            print("DRY RUN - nothing will be copied, changed or deleted")

        result = migrate(session, source, destination,
                         older_than_days=args.older_than_days, camera_id=args.camera,
                         limit=args.limit, dry_run=args.dry_run)
        print(result.report())
        for error in result.errors[:20]:
            print(f"  ! {error}")

        if args.clean_orphans:
            removed = clean_orphans(session, source, dry_run=args.dry_run)
            print(f"orphan files on {source.name}: {removed}"
                  f"{' (dry run)' if args.dry_run else ' removed'}")

    return 0 if result.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
