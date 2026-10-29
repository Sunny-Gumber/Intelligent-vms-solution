# Camvault Forensic Enhancement — Phase 1 Offline Worker

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

## Tests

Run from intelligent-vms-v1/:

    python -m pytest -q tests/test_forensic_enhancement.py

Always-on tests cover bounds, filter order, ROI validation, unsafe output refusal and hashing. If FFmpeg/FFprobe are present, synthetic PNG and MP4 processing tests also verify source preservation, manifest hashes and failed-render cleanup.

## Integration gates remaining

1. Build bounded regional forensic job scheduling with CPU, memory, disk, timeout, retention, cancellation and concurrency quotas, independent of recording/live media.
2. Stage input **only** from existing authorized recording-export APIs. Never expose arbitrary user-provided server paths, URLs or FFmpeg command parameters.
3. Bind jobs to operator identity and enforce tenant/site/camera authorization, audit events and download access.
4. Add original-versus-derivative preview and evidence report verification to the operator UI.
5. Test negative authorization, concurrent operations, disk exhaustion, codec diversity, Windows and Linux packaging, performance and failure isolation.
6. Evaluate optional CLAHE, temporal alignment and AI restoration separately with quality benchmarks and explicit synthetic-detail warnings.
7. Complete security and licence reviews, CI, independent reviewer sign-off and real camera qualification before changing F11-010 from TARGET.

Do not merge/deploy or claim production readiness based solely on this offline worker.
