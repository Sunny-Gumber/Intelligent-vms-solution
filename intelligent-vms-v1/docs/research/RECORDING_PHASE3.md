# Phase 3 recording/playback research

## Decision summary

MediaMTX remains the media recorder, while the VMS owns policy, authorization, health, metadata indexing and later storage orchestration.

### Continuous recording
A dedicated recording path pulls the camera main stream with `sourceOnDemand=false`. The existing live-view path remains independent and can keep `sourceOnDemand=true` on the substream.

This avoids two bad compromises:
- keeping every browser/live substream permanently open just to record; and
- recording the low-resolution substream because the live path happened to use it.

### Container
Use fMP4 source-copy recording. No normal-path decode/transcode.

### Crash RPO
MediaMTX records fMP4 segments as smaller parts. Its documented behavior is that the last part is the loss unit during system failure, so `recordPartDuration` is the recording RPO. Phase 3 default: 1 second.

### Segment duration
Default logical segment duration: 15 minutes.

Why not 1 minute at 100K cameras?
- 100,000 cameras / 60 s ~= 1,667 completed segments/s.
- 15 minutes ~= 111 completed segments/s.

Longer segments reduce metadata/object/file churn while 1-second parts preserve crash RPO. Segment duration remains configurable per recording policy.

### Retention
MediaMTX supports per-path `recordDeleteAfter`. Phase 3 uses this only for the local hot tier and always scopes it to the configured recording path. Later warm/archive lifecycle uses an explicit storage service and must never depend on broad directory deletion.

### Completed-segment hook
MediaMTX exposes:
- MTX_PATH
- MTX_SEGMENT_PATH
- MTX_SEGMENT_DURATION

The VMS will use this hook to publish normalized recording-segment metadata to the event backbone. This creates an efficient searchable/indexable metadata plane without scanning filesystem trees.

### Playback
MediaMTX playback exposes:
- /list?path=...&start=...&end=...
- /get?path=...&start=...&duration=...&format=fmp4|mp4

These endpoints remain internal. Clients call the VMS, which derives the authorized recording path from camera identity and proxies playback. The browser never chooses an arbitrary MediaMTX path.

## Scale rule

At 100K+ channels:
- record locally/regionally;
- central services store policy and searchable metadata;
- playback is routed to the responsible recording node;
- storage movement happens asynchronously;
- a recording-node failure domain must not span all sites.

## Phase 3 limitations

Phase 3 implements continuous recording first.
Schema reserves future modes:
- disabled
- continuous
- event
- scheduled

Event pre/post-buffer and schedule orchestration require later state machines and are not faked in this phase.
