# Database migrations

Production schema is managed with Alembic.

## New database

From `intelligent-vms-v1`:

```bash
export DATABASE_URL='postgresql+asyncpg://...'
alembic upgrade head
```

Production keeps `AUTO_CREATE_SCHEMA=false`.

## Existing Phase-1/2A development database

Older development builds used SQLAlchemy `create_all`. Do **not** blindly run the initial migration against a DB that already contains the same tables.

After verifying the existing schema matches baseline 0001:

```bash
alembic stamp 0001
```

Then all future schema changes use migrations normally.

## Rules

- every schema change gets a migration;
- CI upgrades an empty database to head;
- destructive migrations require backup/rollback planning;
- large data rewrites are separated from application deploys;
- production downgrades are not assumed safe;
- application startup never silently mutates production schema.


## Upgrade / rollback recovery

Before an N-1 -> N production migration, capture and verify the Phase 9.3 backup set.
Schema rollback is not assumed safe. If the N schema is not backward compatible with
the N-1 application, isolate writers and use the tested database restore procedure
instead of forcing an Alembic downgrade.

See `PHASE9_BACKUP_RESTORE_DR.md`.
