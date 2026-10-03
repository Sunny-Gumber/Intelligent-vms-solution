# Phase 6 Architecture — AI Orchestration

AI is isolated from normal media:
Camera -> MediaMTX -> recording/live view; optionally -> AI provider -> normalized AI result -> vms.events.v1 -> search/alarms/UI.

Control plane owns immutable model/provider identity and per-camera AI policy. Providers never choose tenant/site/camera authorization; result ingestion revalidates current policy/model/camera scope.

Camera-metadata mode requires no server decode. Inference mode references an approved model/provider. Local inference runtimes live in separate provider images/packages; the control API never dynamically installs GPU packages.

Phase 7 assigns inference policies to registered AI nodes using measured capacity (decoded pixels/s, FPS, RAM/VRAM and utilization). If no capacity exists, work is held/skipped; recording is never blocked.
