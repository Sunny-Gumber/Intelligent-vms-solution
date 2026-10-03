# AI Orchestration Operations

## Purpose

The VMS core stores AI model/provider identity and camera policies. It does not download arbitrary model files and it does not couple inference to recording.

## Register an approved model/provider

`POST /api/v1/ai/models`

ONNX example:
```json
{
  "tenant_id":"default",
  "name":"person-vehicle-detector",
  "version":"2026.09",
  "provider_type":"onnx",
  "artifact_ref":"model://person-vehicle/2026.09",
  "sha256":"<64 hex characters>",
  "labels":["person","vehicle"],
  "input_width":640,
  "input_height":640
}
```

External provider references use `provider://...`. Arbitrary HTTP model URLs are rejected.

## Camera policy

`PUT /api/v1/ai/cameras/{camera_id}/policy`

```json
{
  "enabled":true,
  "source_mode":"inference",
  "model_id":"<model id>",
  "stream_role":"sub",
  "sample_fps":2,
  "min_confidence":0.6,
  "analytics":["human","vehicle"],
  "zones":[
    {"id":"gate","name":"Gate","kind":"polygon","points":[
      {"x":0.1,"y":0.1},{"x":0.9,"y":0.1},{"x":0.9,"y":0.9},{"x":0.1,"y":0.9}
    ]}
  ],
  "provider_config":{"batch_size":4}
}
```

Provider config is declarative only. Secret-like keys are rejected; credentials belong in deployment secret injection.

## Provider result contract

A service/admin identity submits `POST /api/v1/ai/results`.

The VMS re-checks:
- caller scope;
- camera scope;
- current enabled AI policy;
- current model identity;
- allowed analytic types;
- confidence threshold.

Only accepted detections are normalized into `vms.events.v1`, after which existing ClickHouse search and alarm rules work without AI-specific storage.

## Failure behavior

AI unavailable does not stop:
- camera ingest;
- live view;
- recording;
- playback;
- camera health.

## Runtime providers

Target-specific ONNX Runtime/OpenVINO/CUDA/TensorRT provider images are intentionally separate from the control API. Model/provider hardware qualification occurs in Phase 8.

## Current Phase-6 boundary

This phase makes AI orchestration/provider integration production-safe. It does not ship proprietary model weights or claim a universal detector accuracy. Deployments must mount/approve a compatible provider/model and attach benchmark/accuracy evidence before production certification.
