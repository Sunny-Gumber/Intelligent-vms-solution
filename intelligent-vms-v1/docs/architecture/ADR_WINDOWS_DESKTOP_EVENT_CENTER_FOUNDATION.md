# ADR — Windows Desktop Alarm / Event Center Foundation

Status: software qualification candidate for Issue #17.

## Decision

The VMS server remains the only event truth authority. Enterprise deployments retain the existing transactional outbox/Kafka/ClickHouse event path. The Windows small-site profile gains a minimal PostgreSQL `event_history` store so Event Center does not require Kafka or ClickHouse.

The desktop uses authenticated Control API history pages only. It never accesses PostgreSQL, cameras, ONVIF event endpoints, or raw device payloads directly.

History is bounded to seven days per query, pages are capped at 200 rows, and ordering is `timestamp DESC, event_id DESC`. Stable event ID is the deduplication key. The Windows view retains at most 500 events.

Recent updates use bounded five-second incremental polling with exponential backoff up to 30 seconds. Polling is cancelled outside Event Center, on profile switch, logout, authentication expiry, and shutdown.

Generic event acknowledgement is intentionally absent. The existing alarm-instance acknowledgement API is authoritative for alarm instances only and is not reinterpreted as generic event acknowledgement.

Event-to-Live reuses the accepted live-grid coordinator. Event-to-Playback reuses the accepted recording timeline/playback coordinator and reports recording gaps truthfully.

Physical camera/analytics event compatibility, event latency, and storm capacity remain External Qualification Pending.
