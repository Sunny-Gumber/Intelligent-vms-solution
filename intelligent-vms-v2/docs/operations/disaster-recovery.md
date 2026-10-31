# Disaster Recovery

> **Status: documented procedure, NOT exercised.** No DR drill has been
> performed against this software. Rehearse it on a test system before
> relying on it.

## The one thing people get wrong

A database backup **alone cannot restore this system**.

Camera passwords are encrypted with a master key that deliberately lives
outside the database at `<data_root>/keys/vms-master.key`. Restore the
database without that key and you get a complete inventory of cameras that
the system cannot authenticate to. Every camera password must then be
re-entered by hand.

**Back up the key separately, and store it somewhere other than the database
backup.** Keeping both in one archive means one stolen archive yields both the
inventory and the credentials.

## What to back up

| Item | Location | Frequency | Notes |
| --- | --- | --- | --- |
| Database | PostgreSQL | Daily (hourly WAL for large sites) | Configuration, index, audit |
| **Master key** | `<data_root>/keys/vms-master.key` | **Once, then on rotation** | Without it, camera credentials are lost |
| Configuration | `/etc/intelligent-vms/` | On change | `vms.toml`, TLS certificates |
| Licence | `<data_root>/license.json` | Once | Re-obtainable from the vendor |
| Recorded video | Storage volumes | **Not backed up** | Protected by RAID and retention, not backup — see below |

### Why video is not backed up

A 64-camera site at 30-day retention holds roughly 29 TB. Conventional backup
of that is neither affordable nor fast enough to be useful. The industry
answer, which this product assumes, is:

- RAID for disk failure
- Retention policy for capacity
- Export for the specific footage that matters (evidence)

If a customer requires video backup, the correct design is a second storage
volume with N+N duplicate recording — which is **not implemented** (see
`docs/architecture/ha-design.md`).

## Recovery procedure

### Scenario A — database lost, server intact

```bash
sudo systemctl stop intelligent-vms-recorder intelligent-vms-api
sudo -u postgres dropdb intelligentvms
sudo -u postgres createdb -O vms intelligentvms
sudo -u postgres pg_restore -d intelligentvms /var/backups/intelligent-vms/db-<stamp>.dump

cd /opt/intelligent-vms/database && sudo -u vms alembic upgrade head
sudo systemctl start intelligent-vms-api intelligent-vms-recorder
```

Startup recovery reconciles the index against disk: segments left `writing`
are finalized or marked corrupt, and files present on disk but absent from the
index are reported as orphans.

### Scenario B — complete server loss, storage intact

1. Install the same version on new hardware (`docs/deployment/ubuntu.md`).
2. **Restore the master key first**, to `<data_root>/keys/vms-master.key`
   with `0600` and `vms:vms` ownership.
3. Restore the database and configuration.
4. Re-attach the storage volumes at the **same paths**. Paths are stored in
   the database; mounting elsewhere orphans every recording.
5. Start services and check `/readyz`.
6. Verify playback of known-good footage before declaring recovery complete.

### Scenario C — master key lost

There is no recovery path. This is by design: the key is what makes the
encrypted credentials meaningless to an attacker who steals a database dump.

1. The system starts and generates a **new** key.
2. Every camera shows `unauthorized`.
3. Re-enter each camera's password.
4. Recorded video is unaffected and remains fully playable and exportable.

### Scenario D — storage volume lost

1. The volume transitions to `offline`; an event and alarm are raised.
2. Recording **fails over** to the next usable volume automatically.
3. Recordings on the lost volume remain in the index but resolve to missing
   files. Playback and export report this precisely rather than hanging.
4. After replacing the volume, mark the affected segments as unavailable with
   `tools/storage_migrate.py --mark-missing <volume-id>`.

## Targets to agree with the customer

| Metric | Suggested target | Dominated by |
| --- | --- | --- |
| RPO (configuration) | 24 h | Database backup frequency |
| RPO (video) | 0 | Video is not backed up; failover is immediate |
| RTO (database restore) | < 1 h | Dump size |
| RTO (full rebuild) | < 4 h | OS install and package deployment |

These are **proposals, not measured results.** Measure them during a drill.

## Drill checklist

- [ ] Restore the database to a test host and confirm the camera inventory
- [ ] Confirm camera credentials decrypt after restoring the key
- [ ] Confirm playback of footage recorded before the failure
- [ ] Confirm an export produces a file with a verifiable SHA-256
- [ ] Time each step and record the result here
