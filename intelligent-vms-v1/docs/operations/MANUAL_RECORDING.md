# Durable Recording-Backed Manual Recording

Milestone #292 implements F02-006 as durable operator Start/Stop interval intent over the existing authoritative continuous MAIN recording. It does not start or stop a camera recorder.

## State and ownership

The database stores an opaque session UUID, tenant/site, nullable camera reference, operator subject, ACTIVE/STOPPED state, server UTC start/stop and audit timestamps. One ACTIVE session is allowed per tenant/site/camera/operator, enforced by a partial unique database index. Different operators may independently mark the same camera; one operator may mark multiple cameras.

The creating operator may read/stop/export its session. An authorized admin may manage sessions in scope. Session IDs are not authorization credentials.

ACTIVE transitions only to STOPPED. Export success/failure is deliberately not a session state: Stop history is immutable even when recording coverage/remux later fails.

## Start / Stop / maximum duration

Start requires current admin/operator camera authorization and an active continuous recording policy. Server UTC is authoritative. Retry/double-click returns the existing ACTIVE session; the database uniqueness constraint closes the cross-replica race.

Stop reauthorizes the session, locks its row, and stores server UTC bounded by start plus RECORDING_EXPORT_MAX_DURATION_SECONDS. Duplicate Stop returns the immutable finalized session.

No browser timer or scheduler is authoritative. An ACTIVE row whose maximum duration has elapsed is deterministically finalized at the maximum boundary whenever session APIs observe it. This guarantees the represented interval is bounded even if the browser disappears.

## Recovery and UI

The lightweight workspace queries /manual-recordings/active on refresh. Active state therefore survives process/replica/browser changes. The session is keyed to its original camera and does not follow active-tile, layout or MAIN/SUB/THIRD changes. The workspace reports when sessions remain active on other cameras.

## Recording and export

Manual recording always represents the authoritative MAIN recording interval. Snapshot remains the currently decoded live-role frame, intentionally different. Stop does not validate or alter continuous recording. Download calls the same #291 stream_recording_clip implementation used by timeline-selected export, so gap, single-recording-node, duration, concurrency, timeout, headers and remux behavior remain shared.

Recording may be disabled after Start; retained coverage can still export. Missing coverage fails explicitly. A recording-node/failover boundary remains unsupported and fails explicitly. Export failure does not roll back Stop and may be retried against the same finalized interval.

## Camera deletion

Camera deletion first finalizes ACTIVE sessions at min(server deletion time, maximum interval), then deletes the camera. The FK uses SET NULL so historical interval metadata is not silently erased. Once the camera is deleted, session export returns 410 because current camera authorization/source resolution is no longer possible.

## Security and qualification

Clients never submit Start/Stop timestamps, record_stream_key, MediaMTX path, recording-node URL, RTSP URL, filesystem path or remux arguments. No camera credential/JWT/media secret is stored in session metadata.

Software evidence does not prove codec/audio interoperability, frame-exact clipping, cross-node stitching, production concurrency, WAN/large-file performance, legal admissibility or long-duration production behavior. Product remains Release Candidate / External Qualification Pending.

## History retention limitation

A bounded recent-session API (default 20, maximum 100 per request) supports refresh recovery and retry of the same finalized interval. Session metadata currently follows the control database's administrative retention/backup lifecycle; there is no separate automatic manual-session history purge policy yet. This is documented rather than inventing an unmanaged retention subsystem. Media itself is not duplicated or retained by this table and remains governed by normal recording retention.
