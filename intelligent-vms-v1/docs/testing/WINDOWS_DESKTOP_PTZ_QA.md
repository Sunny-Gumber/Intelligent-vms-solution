# Windows Desktop PTZ Foundation — Independent QA Plan

Issue #14 / PR #15

## Deterministic software QA

The exact PR head must demonstrate capability-driven PTZ; Up/Down/Left/Right plus STOP; optical Zoom In/Out plus STOP when advertised; lost-capture dead-man behavior; bounded command rate; newer STOP preempting an in-flight MOVE; stale MOVE compensation; Camera A to B fencing; fresh client context restart; cleanup on tile/layout/focus/workspace/profile/logout/auth-expiry/deactivation/shutdown; failure isolation; 1/4/9/16 logical layouts; playback isolation; bounded 401 refresh; and redacted diagnostics.

Backend deterministic coverage is in `tests/test_ptz_foundation.py`. Native race/lifecycle coverage is in `clients/windows/tests/IntelligentVMS.Desktop.Tests/PtzTests.cs`.

## Exact-head release gates

Acceptance requires PASS on the final PR head for Intelligent VMS Windows Client, Intelligent VMS CI, Intelligent VMS Security, Ubuntu 22.04/24.04 Field Test, and Windows Server 2022/2025 Field Test.

## Physical QA not claimed

Real camera qualification remains external. Future evidence must record manufacturer, model, firmware and ONVIF profile and exercise pan/tilt, optical zoom, stop latency, disconnect during movement, reboot, latency/loss and representative vendors.