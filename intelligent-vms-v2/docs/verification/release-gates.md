# Release Gates

`PRODUCTION_READY` in `services/control-api/app/version.py` is `False` and
`GET /api/v1/system/version` reports `production_ready: false`.

That flag may only be flipped when **every** gate below has passed with
recorded evidence. This file is the checklist, and it is deliberately not
negotiable by a release date.

## Gate 1 — Hardware compatibility

- [ ] At least 10 camera models from at least 4 manufacturers onboarded via ONVIF
- [ ] At least 3 plain-RTSP cameras onboarded without ONVIF
- [ ] H.264 and H.265 both recorded and played back
- [ ] PTZ verified on at least 3 PTZ models, including preset recall
- [ ] ONVIF event subscription verified on at least 3 models
- [ ] Camera time synchronization verified
- [ ] Compatibility matrix published in `camera-compatibility.md`

## Gate 2 — Endurance

- [ ] 72-hour continuous recording, 16 cameras minimum, zero unexplained gaps
- [ ] Recording continuity ≥ 99.9% measured by the continuity report
- [ ] No memory growth beyond 10% over the run in any process
- [ ] No file-descriptor or subprocess leak
- [ ] Retention observed deleting correctly over a full cycle

## Gate 3 — Scale

- [ ] 64 cameras recording simultaneously on documented hardware
- [ ] 16 simultaneous live viewers
- [ ] 4 concurrent exports under recording load
- [ ] Measured CPU, RAM, disk and network recorded and compared with the
      capacity model; **the model corrected where it is wrong**

## Gate 4 — Failure injection

- [ ] Camera unplugged mid-recording → gap recorded, automatic recovery
- [ ] Network partition to the camera VLAN → recovery without restart
- [ ] Storage volume removed → failover, alarm raised
- [ ] Storage filled to capacity → rotation, no silent data loss
- [ ] Database restarted → reconnection without restart
- [ ] Media gateway killed → live view fails clearly, recording unaffected
- [ ] `SIGKILL` the recorder → startup recovery finalizes or marks corrupt
- [ ] Power loss during recording → system returns to a consistent state
- [ ] Client killed during PTZ → camera stops within the configured window

## Gate 5 — Security

- [ ] Third-party penetration test with findings remediated
- [ ] Dependency audit clean, or every finding risk-accepted in writing
- [ ] TLS configuration scored A or better by an external scanner
- [ ] Authorization matrix verified by someone other than the implementer
- [ ] Credential-at-rest handling reviewed
- [ ] A support bundle inspected and confirmed to contain no secrets

## Gate 6 — Installation

- [ ] Clean install on Windows Server 2022 **and** 2025
- [ ] Clean install on Ubuntu 22.04 **and** 24.04
- [ ] Upgrade from the previous version with recording continuity preserved
- [ ] Interrupted upgrade recovered using the documented procedure
- [ ] Uninstall verified to preserve data
- [ ] Disaster-recovery drill completed and timed

## Gate 7 — Clients

- [ ] Windows client compiles and runs
- [ ] Web client builds and runs
- [ ] 16-tile wall stable for 8 hours
- [ ] One camera failing verified not to affect other tiles
- [ ] Multi-monitor operation verified
- [ ] Accessibility review completed

## Gate 8 — Documentation and commercial

- [ ] Feature matrix reconciled against observed behaviour
- [ ] Every "Partial" either completed or accepted in writing by the product owner
- [ ] Final licence text in place
- [ ] FFmpeg distribution decision made and legally reviewed
- [ ] SBOM published
- [ ] Operator and administrator guides validated by someone who did not write them

## Sign-off

| Gate | Owner | Date | Evidence |
| --- | --- | --- | --- |
| 1 Hardware | | | |
| 2 Endurance | | | |
| 3 Scale | | | |
| 4 Failure | | | |
| 5 Security | | | |
| 6 Installation | | | |
| 7 Clients | | | |
| 8 Docs/commercial | | | |

No gate may be signed by the person who implemented the feature it covers.
