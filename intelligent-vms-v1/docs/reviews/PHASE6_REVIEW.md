# Phase 6 Independent Review — AI Orchestration

Reviewer status: **PASS for merge after CI**.

## Findings fixed during review

1. **Disabling inference policy required a model ID** — fixed. Disabled policy can clear model state without a lookup.
2. **Camera metadata policy semantics were misleading** — fixed. ONVIF/camera analytics remain the independent Phase-5 event path; Phase-6 camera AI policies now mean server/external inference only.
3. **Provider artifact identity** — hardened. ONNX models require `model://` plus SHA-256; external providers require `provider://`. Arbitrary HTTP model URLs are rejected.
4. **Provider config/result bounds** — hardened. Secret-like provider config keys and oversized result attributes are rejected.
5. **Enabled policy without analytics** — rejected.

## Security positives

- tenant-scoped immutable model versions;
- current camera scope and current model/policy are revalidated at result ingest;
- service/admin role required to submit provider results;
- model weights remain outside Git and are not dynamically downloaded by control API;
- provider config is declarative and cannot contain obvious credentials;
- face events can be transported without creating a biometric identity/gallery database;
- bounding boxes and geometry are normalized/bounded.

## Reliability positives

- AI failure does not stop live view, recording, playback or camera health;
- result IDs normalize to deterministic event IDs;
- existing Kafka/event/search/alarm pipeline is reused;
- minimum-confidence and analytic allowlist filters are applied before publication.

## Accepted next-phase responsibilities

### Phase 7
- AI worker/node registry;
- capacity admission and placement;
- leases/fencing;
- provider assignment/failover;
- broker-native high-volume result path where required.

### Phase 8
- exact model/provider images;
- model accuracy evidence;
- decoded FPS/MPix throughput;
- CPU/RAM/VRAM/GPU/NPU sizing;
- thermal/long-duration benchmark.

## Decision

No Phase-6 blocker remains. Merge after migration/unit/full-repo CI are green. Production AI capability remains conditional on an approved provider/model and Phase-8 qualification evidence.
