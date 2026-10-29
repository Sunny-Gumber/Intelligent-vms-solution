# Intelligent VMS Forensic Enhancement â€” Phase 1 Offline Worker

**Status: implementation candidate, not field qualified.** Product feature F11-010 remains TARGET until API/client integration, independent QA, CI verification and external qualification.

## Scope

The standalone Python worker at services/forensic_worker/enhance.py creates CPU-processed derivatives from an **already authorized, locally staged** image or recording clip. It does not open a camera/RTSP source, modify recording, connect to the VMS database, or expose a new server API. The existing VMS control API must remain the sole tenant/site/camera authorization authority.

Implemented: region-of-interest crop; HQDN3D denoising; global histogram equalization (histeq, **not CLAHE**); brightness, contrast, gamma and unsharp masking. All filter settings are range-checked and recorded.

Sources: PNG, JPEG, MP4, MOV, MKV. Outputs: lossless PNG for still images, H.264 MP4 for video. Video derivatives intentionally **exclude audio**. Video processing does not change the original recording.

Dependencies: Python 3.12+ with FFmpeg and FFprobe installed locally. No Python image library, GPU, cloud service, external API, downloaded neural model or camera credentials are required. Confirm the binary supports hqdn3d, histeq, eq, unsharp, PNG and libx264. Before commercial redistribution, review FFmpeg build options and codec/licence obligations.

## Offline proof-of-concept command

Run from intelligent-vms-v1/:

    python services/forensic_worker/enhance.py --input /approved-export/dark.mp4 --output /approved-export/dark-enhanced.mp4 --gamma 1.4 --contrast 1.15 --histogram-strength 0.15 --denoise 2 --sharpen 0.5 --roi 100,100,640,480

For PNG/JPEG inputs, supply a .png output path. The tool writes a .forensic.json manifest beside the new file and refuses to overwrite the original, any existing derivative or an existing manifest.

## Evidence and resource safety

- Input maximum: 512 MiB; decoded resolution maximum: 3840x2160 pixels; video duration maximum: 120 seconds.
- Processing subprocess timeout: 240 seconds; probe timeout: 20 seconds; FFmpeg called with a fixed argument vector and no shell.
- Input must be a local file. URI-like paths are rejected; this worker is not a public API.
- SHA-256 input and derivative hashes are recorded; the input is re-hashed after processing to detect changes.
- Manifest captures source filename, dimensions, duration, UTC creation time, FFmpeg version, filter graph and numeric settings. It does not record full source filesystem paths or credentials.
- Temporary files are removed on handled errors; output and provenance are separate from original media.
- This is an enhancement derivative, **not** original evidence. Contrast correction and sharpening can amplify noise, produce halos and distort detail. No identification, authenticity or court-admissibility claim is made.

## VMS browser playback integration (experimental)

The current VMS browser playback page now loads web/forensic-preview.js and
web/forensic-ui.js. Admin/operator users can inspect one already decoded,
same-origin authorized recording-playback frame using a two-canvas original
versus enhanced preview. Controls include brightness, contrast, gamma and
bounded global luminance histogram equalization. Processing stays in the
operator browser and does not create another RTSP reader, recording source
or backend processing job. The UI invalidates its frame on logout, playback
restart, timeline selection changes and active-camera changes.

The preview supports explicit downloads of the decoded original PNG,
enhanced PNG and a separate JSON report. On a secure browser context,
the JSON contains SHA-256 hashes of the encoded PNGs, filter settings,
camera ID and an approximate (not frame-accurate) playback time. **It does
not hash or authenticate the original MP4**, persist an evidence case,
perform server-side audit, certify identity, or claim legal admissibility.
The JavaScript histogram algorithm differs from FFmpeg's global histeq
filter: matching numeric settings across the two paths are *not* guaranteed
to produce bit-identical outputs. The preview is capped at 1920x1080 and
never silently upscales or downscales the captured frame. This panel is
a first operational UX, not the production forensic engine.

The browser regression test is tests/forensic_preview_test.cjs. Run it from
intelligent-vms-v1/ with:

    node tests/forensic_preview_test.cjs

## Tests

Run from intelligent-vms-v1/:

    python -m pytest -q tests/test_forensic_enhancement.py

Always-on tests cover bounds, filter order, ROI validation, unsafe output refusal and hashing. If FFmpeg/FFprobe are present, synthetic PNG and MP4 processing tests also verify source preservation, manifest hashes and failed-render cleanup.

## Integration gates remaining

1. Build bounded regional forensic job scheduling with CPU, memory, disk, timeout, retention, cancellation and concurrency quotas, independent of recording/live media.
2. Stage input **only** from existing authorized recording-export APIs. Never expose arbitrary user-provided server paths, URLs or FFmpeg command parameters.
3. Bind jobs to operator identity and enforce tenant/site/camera authorization, audit events and download access.
4. Connect the server-side worker to an authorized queued VMS job and add tamper-evident case provenance. The current browser preview is not a substitute.
5. Test negative authorization, concurrent operations, disk exhaustion, codec diversity, Windows and Linux packaging, performance and failure isolation.
6. Evaluate optional CLAHE, temporal alignment and AI restoration separately with quality benchmarks and explicit synthetic-detail warnings.
7. Complete security and licence reviews, CI, independent reviewer sign-off and real camera qualification before changing F11-010 from TARGET.

Do not merge/deploy or claim production readiness based solely on this offline worker.


## Server job continuation — 2026-10-29

An opt-in `/api/v1/forensic/jobs` API stages a complete clip through the existing
recording export authorization path. Every job operation rechecks owner and current
camera tenant/site authorization. Requests accept bounded numeric settings, never
server paths, URLs, executable names or arbitrary filter graphs.

The experimental single-process executor retains at most four jobs, two per tenant,
one per operator, and processes one at a time. Retained jobs consume admission slots
until explicitly deleted; it rejects admission rather than silently evicting evidence.
A private spool and immutable worker image digest must be configured explicitly.
The feature is disabled by default. A dedicated Docker host is required; do not give
a production control API access to a shared recording/media Docker daemon.

The worker container has no network, a read-only source mount/root filesystem,
1 CPU, 512 MiB memory, 64 PIDs and bounded temporary filesystems. API export staging
has a 60-second deadline and 512 MiB source ceiling. Cancellation waits for container
removal; uncertain removal prevents deletion/admission reuse. Restart with spool
leftovers fails closed for administrator recovery. This is not a distributed durable
scheduler or an immutable evidence vault. Forward audit logs to the organization's
retained audit sink before enabling it. Deletion removes local job files.

Browser preview now supports ROI selection in captured original pixel coordinates.
Reports bind ROI, algorithm version/settings, recording interval and approximate
playback offset to exported PNG hashes. Preview and FFmpeg algorithms remain distinct.
Hashes support integrity comparison, not authenticity or legal admissibility.

Worker hardening restricts demuxers/protocols, bounds output/threads/frames/time,
rejects shortened clips and publishes without clobbering competing output files.
The frontend gate now parses approved local forensic scripts; unapproved/remote
script URLs still fail closed. Forensic CI includes Ubuntu and Windows jobs.

Qualification still requires actual isolated-container execution, green latest-head
CI/security/field tests, independent review, named-camera evidence and licence
approval for the exact FFmpeg build. No deployment or production qualification is
claimed. The development worker image recipe is not an approved distributed binary.


Configuration: FORENSIC_JOBS_ENABLED=false (default), FORENSIC_SPOOL_DIR
(existing empty private absolute path), FORENSIC_WORKER_IMAGE (immutable digest,
already built/reviewed/present). Browser controls support create, recover owned
jobs, refresh, cancel, report/artifact download and delete without polling.

Windows Python3.12 combined forensic/API/frontend/export/FIX038 run:74 passed,
1 PostgreSQL skipped (database absent). Node algorithm and DOM interaction tests,
Ruff, public docstrings and compile checks passed. Full suite attempted:109 passes
then child-process Windows _overlapped initialization failed with WinError10106;
this is not a full-suite PASS. Docker/WSL/PostgreSQL unavailable locally.

Local FFmpeg9.0.2 gyan.dev full build reports --enable-gpl, --enable-version3,
--enable-libx264. No binary redistributed. Exact-image licence notices/source
offer, codec obligations and owner approval remain release gates.


Additional verified fixes: MediaMTX rebuilt using Go1.26.10 and x/net0.60.0;
two distinct source/work/compiler-cache builds on this Windows host produced
identical ZIP5268c43c2643ddbc1ceeefc5ea26489e65055be43cc4d785bf5af7fcd9b26401
and EXE566a0d875950527e12bfb78a4fb8206b75cbcd2b137d86a33082aedd73d43db8.
Manifest and installer pins came from actual outputs. Runtime transport/TLS/
redirect confinement check PASS. Independent cross-host reproducibility and a
fresh hosted security scan remain gates. Node fence-state persistence now uses
existing cross-platform directory flush; focused Windows durability/fencing/
autonomy tests33PASS. Combined forensic/integrity regressions80PASS1PostgresSKIP.
The optional real Docker regression runs on Linux CI; it skips locally because
Docker is absent. A separate playback regression run had50PASS1FAIL: closed-port
connect returned ConnectTimeout on this host where test expects ConnectError.
This unresolved platform-dependent test is not reported as a passing suite.


Final local batch:86 passed,2 skipped (real Docker and PostgreSQL absent),
1 upstream TestClient deprecation warning. Phase7 chaos gate PASS14/14 after
Windows flush fix, with absolute RECORDING_MOUNT_PATH configured. MediaMTX
integrity/Windows installer suite59PASS. No full-suite/hosted-security PASS claim.
FORENSIC_WORKER_IMAGE accepts a reviewed immutable repository digest or local
sha256 image ID. CI builds the worker locally and exercises real container
resource controls, source hashes, completion, cancellation and removal on Linux.
