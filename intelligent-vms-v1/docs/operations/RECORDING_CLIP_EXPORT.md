# Bounded Recording-Range Clip Export

Milestone: #290
Parent: #221
Primary feature: F02-021
Related: F02-020
Audited but not implemented: F02-006
Product status: Release Candidate / External Qualification Pending

## Decision

The authoritative catalog names F02-006 only as "Manual recording". It does not establish that Start/Stop button semantics and clip export are the same requirement. This milestone implements the prerequisite already supported safely by the recording architecture: bounded recording-range clip export/download. F02-006 remains a separate target until durable manual-intent semantics are designed and reviewed.

Browser MediaRecorder is rejected. It would capture the decoded workstation live role, depend on tab/device lifecycle, and create a second non-authoritative recording path. A clip is instead streamed from the existing continuous MAIN recording via record_stream_key and MediaMTX playback.

## Recording and container architecture

Continuous recording remains MAIN/source-copy, record=true, sourceOnDemand=false, recordFormat=fmp4, with configured fMP4 part and segment durations. MediaMTX stores segments under the controlled recording path template. Export asks the existing playback service for MP4; no FFmpeg, transcoder, second RTSP connection, second MediaMTX recorder or temporary export file is introduced.

The playback/remux service determines decodable/keyframe cut behavior. This milestone does not claim frame-exact boundaries or universal MP4/audio codec compatibility.

## API and authorization

GET /api/v1/recordings/cameras/{camera_id}/export?start=<timezone-aware>&duration=<seconds>

Only admin and operator roles may export. The request is authorized against the camera on every download request. The server resolves recording policy, record_stream_key, tenant/site identity and recording node. Clients cannot provide RTSP/HTTP/file URLs, MediaMTX paths, filesystem paths, output paths or remux flags.

The response is video/mp4 with an attachment filename generated from a sanitized stable camera ID and UTC start/end timestamps. It carries nosniff and private/no-store cache policy. There is no public/signed export URL and no export ID to enumerate.

## Coverage and gaps

The entire requested interval must have continuous authoritative coverage. Any gap fails explicitly with 409; missing time is never silently removed. In distributed mode this first export contract uses finalized indexed recording segments and requires the entire range to belong to exactly one recording node. A failover/node boundary is rejected instead of truncating or stitching across nodes. Very recent, not-yet-finalized distributed footage is not exportable until indexed.

A selected interval may cross multiple finalized segments on the same node. MediaMTX playback performs the bounded MP4 retrieval across its recording data.

## Bounds

RECORDING_EXPORT_MAX_DURATION_SECONDS defaults to 900 seconds and is configurable up to four hours. This is a safety guardrail, not a measured production-capacity claim.

RECORDING_EXPORT_MAX_CONCURRENT_PER_PROCESS defaults to 4. A process-local semaphore is held for the full upstream/downstream stream and released on open failure or response completion/disconnect. This is explicitly not a cluster-wide quota. Multi-replica aggregate export capacity must be controlled by deployment/ingress sizing until a distributed export scheduler exists.

Because export streams existing media and creates no worker/subprocess/temp file, there is no export-file expiry or orphan-file cleanup. Client disconnect closes both upstream HTTP response and client and releases the slot.

## UI

Playback timeline selection remains authoritative. Selecting a recorded span enables "Download selected clip". The request uses the selected camera/start/duration; changing active live tile or MAIN/SUB/THIRD role cannot redirect or change its recording source.

This milestone does not add Start/Stop manual-recording intent. Therefore reload/navigation has no hidden manual session to orphan.

## Recording, live, AI and snapshot invariants

Export never mutates recording policy, record_stream_key, source URI, retention, placement, fencing, completion hooks or MediaMTX recording configuration. Export failure cannot stop continuous recording. It does not create/change WHEP sessions, live maxReaders, viewer role, AI policy/source, or #289 snapshot behavior.

## Qualification boundary

Software CI can verify authorization wiring, time/resource bounds, continuous-coverage policy, node-boundary rejection, safe headers, cleanup contracts and subsystem separation. Real camera codecs, MediaMTX remux behavior for every codec/audio combination, frame-exact cuts, large concurrent/large-file/WAN performance, long-duration stability, multi-node stitching and legal/evidence admissibility remain NV / External Qualification Pending.
