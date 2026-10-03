# Phase 9.3 Backup, Restore, DR and Upgrade/Rollback

## Scope

This runbook is the executable Phase 9.3 recovery baseline for the Intelligent VMS.

It covers:

- PostgreSQL control-plane backup and restore;
- ClickHouse event/recording-metadata backup and restore;
- regional node fencing state and regional spool backup/restore;
- Helm release configuration backup;
- recording/object-storage lifecycle boundaries;
- region-loss recovery exercises;
- N-1 -> N upgrade validation and rollback;
- measured exercise RPO/RTO evidence.

The scripts do not create a production RPO/RTO guarantee. Production targets remain
operator/SRE objectives until repeated exercises on the selected infrastructure prove
that they are achievable.

## State ownership

PostgreSQL is durable control truth. It contains cameras, configuration, encrypted
credential fields, recording policies, health/alarm/AI configuration, placement,
fencing/revocation state and the transactional outbox.

ClickHouse contains searchable event history and completed recording-segment metadata.
Loss of ClickHouse does not redefine camera ownership, but it can reduce searchable
history until restored or rebuilt.

Regional node state contains the locally persisted fencing snapshot. Regional spool
SQLite contains events, recording hooks and heartbeats that were accepted during WAN
or central-control loss and have not yet been delivered.

Media recordings are separate payload data. Database backup does not back up fMP4
recording files or archive objects.

Helm backup captures release values/manifests/history. The chart references existing
Secrets; do not export plaintext Secret objects into ordinary backup artifacts.
Back up secret-manager/KMS material using the selected secret platform's protected
backup process.

## Operator scripts

All scripts are under `deploy/operations`.

### PostgreSQL backup

Run with the production PostgreSQL client tools available:

```bash
export PGSERVICE=vms-backup
export PGSERVICEFILE=/run/secrets/pg_service.conf
export PGPASSFILE=/run/secrets/pgpass
export BACKUP_DIR=/secure-backups/vms
export BACKUP_ID=20260928T020000Z
bash deploy/operations/postgres-backup.sh
```

The script creates a custom-format `pg_dump`, a SHA-256 checksum and validates that
`pg_restore --list` can read the dump. Use libpq environment/service configuration
(`PGSERVICE`/`PGSERVICEFILE`/`PGPASSFILE`, or `PGDATABASE` plus the standard PG
environment) so credentials are not placed in process arguments or logs.

For a non-destructive validation only:

```bash
export PGSERVICE=vms-restore-verify
export PGSERVICEFILE=/run/secrets/pg_service.conf
export PGPASSFILE=/run/secrets/pgpass
export BACKUP_FILE=/secure-backups/vms/20260928T020000Z/postgres/database.dump
export VALIDATE_ONLY=1
bash deploy/operations/postgres-restore.sh
```

A destructive restore requires `CONFIRM_RESTORE=YES`. Restore into an isolated/clean
database first whenever possible.

### ClickHouse backup

ClickHouse native backup requires a configured backup disk. Credentials should be
provided through a protected clickhouse-client configuration file rather than command
arguments.

```bash
export CLICKHOUSE_HOST=clickhouse.internal
export CLICKHOUSE_NATIVE_PORT=9000
export CLICKHOUSE_USER=backup_operator
export CLICKHOUSE_CONFIG_FILE=/run/secrets/clickhouse-client.xml
export CLICKHOUSE_DATABASE=vms
export CLICKHOUSE_BACKUP_DISK=backups
export BACKUP_ID=20260928T020000Z
bash deploy/operations/clickhouse-backup.sh
```

Restore to a verification database first:

```bash
export CLICKHOUSE_TARGET_DATABASE=vms_restore_verify
bash deploy/operations/clickhouse-restore.sh
```

Restoring to the original database name requires explicit
`CONFIRM_RESTORE=YES` and a clean target appropriate for ClickHouse RESTORE.

### Regional state backup

Run the regional-state backup from a maintenance context where the node-state and spool
volumes are both mounted:

```bash
export BACKUP_DIR=/secure-backups/vms
export BACKUP_ID=20260928T020000Z
export REGIONAL_NODE_STATE_PATH=/var/lib/vms-node/fence-state.json
export REGIONAL_SPOOL_DB_PATH=/var/lib/vms-spool/spool.db
bash deploy/operations/regional-state-backup.sh
```

SQLite is copied through the SQLite backup API rather than a raw live-file copy. The
backup directory contains `SHA256SUMS`.

Before regional restore:

1. stop the node-agent and regional-spool workloads for that node identity;
2. confirm the node is not actively executing a newer placement generation;
3. verify the selected backup/checksums;
4. set `CONFIRM_RESTORE=YES`;
5. restore the files;
6. restart the regional workloads and verify the central fence snapshot before media
   or recording ownership is accepted.

A stale regional state backup must never be used to override a newer central placement
generation.

### Helm release configuration backup

```bash
export RELEASE_NAME=vms
export NAMESPACE=vms
export BACKUP_DIR=/secure-backups/vms
export BACKUP_ID=20260928T020000Z
bash deploy/operations/helm-config-backup.sh
```

The output contains Helm values, rendered manifest and release history plus checksums.
It intentionally does not export Kubernetes Secret objects.

## Backup-set manifest verification

Generate a content manifest after the backup set is complete:

```bash
python tools/phase9_dr_evidence.py manifest \
  --root /secure-backups/vms/20260928T020000Z \
  --output /secure-backups/vms/20260928T020000Z.manifest.json
```

Verify before every restore/exercise:

```bash
python tools/phase9_dr_evidence.py verify \
  --root /secure-backups/vms/20260928T020000Z \
  --manifest /secure-backups/vms/20260928T020000Z.manifest.json
```

The manifest records file names, byte sizes and SHA-256 hashes only. It does not copy
backup contents or credentials into evidence JSON.

## Restore order

For full control-plane recovery, use this order:

1. isolate or stop writers to the failed target environment;
2. verify the backup-set manifest;
3. restore PostgreSQL into a clean target and run `alembic current` / application
   schema compatibility checks;
4. restore ClickHouse into a verification database and validate event/segment queries;
5. restore external secret-manager/KMS material through its protected process;
6. reinstall/restore the Helm release from immutable image tags and backed-up values;
7. restore regional state only for node identities that require it and only while
   those workloads are stopped;
8. start control API/workers, then regional agents/spools;
9. verify readiness, placement generations, pending revocations, outbox depth,
   regional spool drain, event search and recording-health signals;
10. release traffic only after validation passes.

Do not restore stale PostgreSQL placement/fence truth over a newer live control plane.

## Region-loss exercise

A region-loss exercise should verify software behavior, not just pod availability.

Required observations:

- affected node heartbeat becomes stale;
- stale node cannot receive renewed placement ownership;
- failover creates a newer generation and durable cleanup/revocation obligation;
- unaffected regions continue control/event operation;
- regional spool remains bounded during WAN isolation;
- restored region cannot resume stale ownership;
- pending regional spool data drains idempotently after connectivity returns;
- recording gaps are measured and reported rather than hidden;
- old-node cleanup completes when the stale region returns.

Capture the failure time, latest usable restore point and service-restored time with the
Phase 9 evidence tool.

## Recording and object-storage lifecycle

Hot recording files remain regional. Protect those volumes with the selected storage
platform's snapshot/replication mechanism and test restore on the actual filesystem or
storage appliance.

For warm/archive object storage:

- completed segment metadata is the transfer unit;
- calculate/checksum before upload;
- verify the remote object before declaring archive availability;
- retain the local hot copy until policy permits deletion;
- legal hold overrides retention;
- object-lock/immutability policy must be configured in the chosen object store;
- lifecycle rules must never delete the last verified required copy;
- database restore alone does not restore video payload objects.

Storage durability, cross-region replication and retrieval timing are provider-specific
and must be validated externally.

## N-1 -> N upgrade validation

Before upgrading:

1. record current Helm revision and immutable image tags;
2. run PostgreSQL, ClickHouse, regional-state and Helm configuration backups as
   applicable;
3. generate and verify the backup-set manifest;
4. review the new Alembic migration for backward compatibility and rollback impact;
5. render/lint the new release:

```bash
export CHART_PATH=deploy/helm/intelligent-vms
export VALUES_FILE=values-production.yaml
export RELEASE_NAME=vms
export NAMESPACE=vms
bash deploy/operations/upgrade-preflight.sh
```

Then perform the controlled upgrade:

1. run the approved Alembic migration step explicitly;
2. run `helm upgrade --install` with immutable image tags, `--atomic`, `--wait`
   and an operator-approved timeout;
3. verify control API readiness and metrics;
4. verify outbox delivery, placement controller, node heartbeats and recording health;
5. verify event and playback queries;
6. record completion in DR/upgrade evidence.

## Rollback decision

Application-only rollback is permitted only when the N schema remains compatible with
the N-1 application.

When schema is backward compatible:

1. `helm rollback <release> <previous-revision> --wait`;
2. verify readiness and worker health;
3. verify placement/fencing and recording-health signals.

When schema is not backward compatible, do not run an unsafe Alembic downgrade merely
to make the old application start. Use the approved recovery plan: isolate writers,
restore the pre-upgrade PostgreSQL backup into a clean target, restore dependent
metadata/configuration as required, deploy the N-1 immutable release, and validate
before traffic is restored.

## RPO/RTO exercise evidence

Example:

```bash
python tools/phase9_dr_evidence.py exercise \
  --exercise-id region-loss-20260928 \
  --exercise-type region-loss \
  --failure-at 2026-09-28T02:00:00Z \
  --restore-point-at 2026-09-28T01:59:00Z \
  --service-restored-at 2026-09-28T02:08:00Z \
  --validation pass \
  --target-rpo-seconds 120 \
  --target-rto-seconds 600 \
  --output evidence/region-loss-20260928.json
```

The tool calculates:

- observed RPO = failure time minus latest restorable data time;
- observed RTO = service-restored time minus failure time;
- whether the supplied exercise targets were met.

Every output includes
`EXERCISE_EVIDENCE_ONLY_NOT_A_PRODUCTION_RPO_RTO_GUARANTEE`.

A single passing exercise is not a production guarantee. Repeat on the selected
production-like infrastructure and retain the evidence set.

## Phase 9.3 software exit gate

The software milestone can pass when:

- operator backup/restore scripts are syntax-checked in CI;
- deterministic DR evidence tests pass;
- backup manifest generation/verification passes in CI;
- upgrade preflight renders the chart;
- the runbook covers PostgreSQL, ClickHouse, regional state, Helm configuration,
  recording/object lifecycle, region loss, upgrade and rollback.

External RPO/RTO, storage durability and regional failover qualification remain pending
until exercises are run on the actual target infrastructure.
