# ADR-0003: Split control and event data stores

## Decision
- PostgreSQL: control-plane truth (tenants, sites, cameras, users, policies, assignments).
- ClickHouse: high-volume time/event metadata.
- Object/file storage: media, snapshots, exports.

## Reason
The workloads have different consistency, ingest and query characteristics. One database should not be forced to serve all three.
