# Phase 3 recording, retention and playback design

## Paths

Each camera has two independent media purposes:

```text
Camera main stream ----> <stream_key>-record ----> recorder ----> /recordings
Camera sub stream  ----> <stream_key>        ----> WebRTC/HLS viewers
```

If no usable substream exists, live view can still use main. Recording always uses the main URI.

## Control-plane recording policy

One RecordingPolicy per camera:
- mode
- enabled
- record_stream_key
- recording_node_id
- retention_days
- part_duration_ms
- segment_duration_seconds
- max_part_size_mb

Phase 3 accepts `continuous` and `disabled`. Future event/scheduled values are schema-compatible but API rejects activation until implemented.

## MediaMTX recording path

Example desired config:

```yaml
source: rtsp://camera-main
sourceOnDemand: false
rtspTransport: tcp
record: true
recordPath: /recordings/%path/%Y/%m/%d/%H/%s-%f
recordFormat: fmp4
recordPartDuration: 1s
recordMaxPartSize: 50M
recordSegmentDuration: 15m
recordDeleteAfter: 7d
runOnRecordSegmentComplete: <internal hook>
```

The live path remains source-on-demand and record=false.

Owner decision C3 (accepted 2026-10-08): every default and template that enables playback includes `%f` in `recordPath`. Pinned MediaMTX v1.21.1 rejects a playback path that omits it. `%s-%f` keeps the Unix-second filename prefix and six-digit microseconds. See `docs/operations/RECORDING_STORAGE.md`.

## Segment metadata plane

Completed segment hook:
MediaMTX -> internal hook endpoint -> Kafka topic `vms.recordings.v1` -> ClickHouse `recording_segments`.

Segment ID is deterministic from recording node + recording path + segment path. Duplicate hook deliveries are safe at consumer/query level.

Fields:
- segment_id
- tenant_id/site_id/camera_id
- recording_node_id
- record_stream_key
- local_segment_path
- segment_start
- duration_seconds
- completed_at
- storage_tier
- object_uri (future)
- indexed_at

## Timeline

Two sources serve different purposes:

1. Node-authoritative current availability:
   MediaMTX playback /list.
2. Central searchable/history/health index:
   ClickHouse recording_segments.

Phase 3 playback API uses the node-authoritative list for actual playable spans. The segment index provides gap analysis, capacity/history and later cross-node search.

## Historical covering lookup

Distributed playback resolves one completed segment before it chooses a recorder.
`segment_for_start` asks ClickHouse for rows that contain the requested instant
and only then applies `LIMIT`. The half-open interval matches the recording
fence: `segment_start <= t < segment_end`. The start instant is inside. The end
instant, and one microsecond later, is outside.

A completed segment can be at most one day, the same maximum the recording hook
accepts. The indexed range is `t - 1 day <= segment_start <= t`, which the
`recording_segments` primary key already serves:

```text
ORDER BY (tenant_id, site_id, camera_id, segment_start, segment_id)
```

No extra index migration is required. The table remains `DateTime64(3)`; widening
that key to microseconds would be a column-type change and is not part of this
lookup fix. The query expression still uses microsecond precision, and Python
re-checks the returned timestamps at microsecond resolution.

When a placement move leaves two completed segments over the same instant, the
winner is the greatest `segment_start`, then the greatest `segment_id`. The
camera's current node is not a tie-break. An instant that only the previous
owner recorded returns that owner's node. An instant in a gap returns no
segment. Playback then asks the current recorder and streams only if that
recorder proves coverage. It does not substitute the current node for a
historical owner.

Timeline and export use `segments()`. That query also applies the overlap
predicate before `LIMIT`, then walks oldest first by `(segment_start, segment_id)`.
Callers page with `after_segment_start` and `after_segment_id`. The HTTP timeline
and export routes take one page, capped by `recording_query_max_segments`, and
do not expose the cursor. That cap is not the malformed-row partial flag. Export
still rejects a range the returned page does not cover continuously.

## Secure playback

Client:
`GET /api/v1/recordings/cameras/{id}/play?start=...&duration=...&format=mp4`

Control API:
1. authenticates user;
2. checks tenant/site access;
3. verifies recording policy;
4. derives the record path;
5. clamps duration;
6. proxies the internal playback response.

No arbitrary user-supplied MediaMTX path or filesystem path is accepted.

## Reconciliation

The existing desired-state reconciler restores:
- live view path; and
- enabled continuous recording path.

Recording path changes are bounded by the same reconnect budget.

## Retention safety

Phase 3 hot retention is per MediaMTX path using `recordDeleteAfter`.

Future archive/tiering:
- final segment event enters storage queue;
- copy/checksum/verify;
- atomically mark object available;
- only then local deletion if policy allows;
- never issue broad recursive delete from user-provided path.

## Recording health

A camera is considered recording-healthy when:
- policy enabled;
- recording config path exists;
- source is ready when expected;
- recent completed segment observed within threshold.

Phase 3 exposes policy/timeline and index. A later health worker converts stale segment observations into alarms.

## 100K scale

Recording policy stays in PostgreSQL control plane; segment metadata is not stored there.

At 15-minute segments and 100K continuously recording cameras:
~111 completed-segment metadata messages/s on average.

Regional recording nodes own media/disk. Central ClickHouse can receive normalized metadata, but video itself does not traverse the central control plane.
