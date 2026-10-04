# Windows Desktop Event Center — Independent Security Review

Issue #17.

Review requirements:
- server-authoritative event identity/time/type/source/severity;
- tenant/site/camera authorization on history and ingestion;
- no desktop PostgreSQL access;
- no camera credentials/RTSP/ONVIF endpoints in Event Center;
- bounded time range/page size/polling/memory;
- stable event-ID deduplication;
- safe text-only metadata rendering;
- no generic client-local acknowledgement;
- profile/logout/auth-expiry cancellation and no stale cross-profile insertion;
- event-to-Live and event-to-Playback reauthorize through existing server paths;
- diagnostics contain state/count/error categories only, not raw metadata or tokens.

Residual hardware/device/event compatibility remains External Qualification Pending.
