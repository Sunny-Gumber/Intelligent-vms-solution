# External qualification plan

The software cannot be called production-ready until the activities below are
performed on real hardware. Each is listed with what it proves and what would
constitute a pass. Nothing here has been done.

## Gate 1 — Camera compatibility

| Test | Pass criteria |
| --- | --- |
| Onboard 10+ camera models from 3+ vendors via ONVIF | Device info, profiles and stream URIs retrieved; capabilities reported match the datasheet |
| Onboard 5+ plain-RTSP cameras | ffprobe reports correct codec/resolution; recording starts |
| Cameras with wrong system clocks | Digest auth still succeeds (clock-skew compensation) |
| Cameras with `@ : /` in passwords | URL encoding correct; stream opens |
| PTZ on 3+ PTZ models | Continuous, preset recall and Stop all behave; **no runaway after the client is killed** |
| ONVIF PullPoint events on 3+ models | Motion/tamper events ingested and attributed `camera_edge` |

## Gate 2 — Recording reliability

| Test | Pass criteria |
| --- | --- |
| 72-hour continuous soak, 16+ cameras | Continuity >= 99.9%; every gap has a matching event; no unbounded memory growth |
| Pull the network cable mid-recording | Recorder retries with bounded backoff; recovers; the gap is recorded |
| `kill -9` the recorder mid-segment | Startup recovery finalizes or marks the segment; never leaves `writing` |
| Fill the storage volume | Emergency rotation runs; a storage event is raised; recording continues |
| Yank the storage device | Volume goes offline; a critical event is raised; failover to another volume |

## Gate 3 — Media delivery

| Test | Pass criteria |
| --- | --- |
| MediaMTX + 16 concurrent browser viewers | Streams play; glass-to-glass latency measured and recorded (no figure is claimed today) |
| Kill MediaMTX while viewers are connected | Live view returns 503 with `media_gateway_unavailable`; **recording is unaffected** |
| Revoke a user mid-stream | Gateway readers are kicked |

## Gate 4 — Platform and installation

| Test | Pass criteria |
| --- | --- |
| Clean install on Windows Server 2022 and 2025 | Services start, survive reboot, correct ACLs on ProgramData |
| Clean install on Ubuntu 22.04 and 24.04 | systemd units start and survive reboot |
| PostgreSQL 15/16 deployment | Migration applies; full test suite passes against PostgreSQL |
| Upgrade from 0.1.0 to the next build | Migration applies; no data loss; documented rollback works |
| Interrupt an upgrade midway | System returns to a known state |
| `dotnet build` the WPF client | Compiles; connects; renders video |
| `npm run build` the web client | Builds; connects; renders WebRTC |

## Gate 5 — Security

| Test | Pass criteria |
| --- | --- |
| Independent penetration test | No critical or high findings outstanding |
| SSRF probe with malicious camera URLs and DNS rebinding | All blocked by NetworkGuard |
| Authorization matrix review | No endpoint reachable outside its permission and scope |
| Secret-leak scan of logs, diagnostics bundle and API responses | No credentials present |
| Dependency CVE scan | No unpatched critical CVEs |

## Gate 6 — Disaster recovery

| Test | Pass criteria |
| --- | --- |
| Restore database + master key onto a clean host | Cameras reconnect; stored credentials decrypt; recordings play |
| Restore WITHOUT the master key | Failure is explicit and understood (credentials unreadable by design) |

## Evidence from the build environment

What WAS executed here is recorded in `docs/verification/test-run.txt`:
**140 automated tests, all passing**, including real FFmpeg recording that
produced playable MP4 files, a real export whose SHA-256 was independently
re-verified, crash recovery, retention deletion, and the full authorization
matrix. That is a necessary foundation, not a substitute for the gates above.
