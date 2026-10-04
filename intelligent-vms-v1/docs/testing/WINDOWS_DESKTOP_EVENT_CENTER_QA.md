# Windows Desktop Event Center — Independent QA Plan

Issue #17.

Deterministic acceptance covers bounded history, stable ordering, pagination, time/camera/type/severity filters, unknown event representation, duplicate suppression by event ID, bounded 500-row view memory, recent polling/backoff, profile/logout/auth/shutdown cleanup, safe metadata text, event-to-Live, event-to-Playback, and recording-gap truth.

Exact-head gates required: Windows Client, Intelligent VMS CI, Security, Ubuntu 22.04/24.04, Windows Server 2022/2025.

Real camera event generation, analytics semantics, event latency, and event-storm capacity are not software-CI claims.
