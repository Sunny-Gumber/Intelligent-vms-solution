# Active-Tile Live Snapshot

**Milestone:** #288  
**Parent:** #221  
**Features:** F02-005; snapshot portions of F02-020 and F02-021  
**Product status:** Release Candidate / External Qualification Pending

## Architecture

The snapshot is captured from the already-authorized decoded video element of the active #284/#286 tile. It therefore captures the **currently viewed MAIN, SUB or THIRD role** exactly as selected by the operator. It is not described as camera-native or MAIN resolution unless MAIN is the selected role.

The browser copies one current decoded frame to an ephemeral canvas, encodes PNG, creates a temporary object URL, triggers a local download, and immediately revokes that URL. There is no server snapshot endpoint, extra MediaMTX reader, camera snapshot URI, RTSP credential exposure, FFmpeg process, temporary server file, database row or persistent VMS evidence repository.

This design is deliberately scoped to live snapshot/local download. It does not claim evidence-grade chain of custody or original sensor-quality capture.

## Authorization and isolation

A snapshot control exists only for the active assigned tile. Capture requires a current LIVE session whose camera ID, selected role and generation match the current workspace state. That session was established only after the existing tenant/site/camera authorization and #278 exact-path grant. The client cannot submit an arbitrary RTSP/HTTP URL, MediaMTX path or filesystem path for capture.

Changing camera, role, layout or authorization inventory invalidates the tile generation/session. If capture encoding finishes after the source changed, the result is rejected rather than mislabeled as the new camera or role.

## Filename and timestamp

Downloads use PNG and a generated filename composed from a sanitized stable camera ID, the exact viewed role and browser UTC request-completion timestamp. Camera/site names are not used as filesystem paths. This timestamp is an operator capture timestamp; it is not claimed to be frame-accurate camera time.

## Resource and failure behavior

Snapshot reuses the existing reader and therefore creates no additional MediaMTX reader and no #281 bypass. Duplicate snapshot clicks on one tile are suppressed while capture is in progress. Failure is tile-local and does not tear down other tiles or mutate recording/AI. Object URLs are revoked after the download attempt. No server process or temporary file exists to orphan.

## Recording and AI invariants

Snapshot does not call recording or AI mutation APIs. Continuous recording remains independent on the MAIN source through its record_stream_key, with existing retention, placement, generation/fencing and completion behavior. Viewer role and snapshot operations do not change persisted AI stream_role.

## Remaining clip/export direction

F02-006 and the clip portion of F02-021 remain separate. Existing authorized bounded recording playback already resolves an authorized camera to its recording policy and record_stream_key and supports bounded MP4/fMP4 ranges. The next clip milestone should reuse that authoritative MAIN recording path and prefer source-copy/remux/export semantics rather than browser MediaRecorder. Browser MediaRecorder is rejected as the architectural default because it would depend on workstation/tab lifecycle, capture decoded live quality, create inconsistent output and duplicate the VMS recording boundary.

## Qualification boundary

CI can verify state association, source-role semantics, safe filename construction, lack of extra reader/server target, cleanup and recording/AI separation. Real camera/browser codec behavior, canvas/browser implementation differences, original sensor resolution, frame-accurate camera timestamp, legal admissibility and workstation performance remain NV / External Qualification Pending.
