# Phase 9.2 — Observability, Alerts and SLOs

Status: software observability baseline. This document defines operational objectives and alert policy; it does **not** claim that production SLOs, hardware capacity, camera interoperability, or site-specific thresholds have been externally qualified.

## 1. Metrics sources

The control API exports low-cardinality Prometheus metrics at `/metrics`.

| Area | Metric | Meaning |
| --- | --- | --- |
| API | `intelligent_vms_http_requests_total` | Request count by bounded route template/status |
| API | `intelligent_vms_http_request_duration_seconds` | Request latency histogram |
| DB/readiness | `intelligent_vms_ready` | Required durable-state refresh succeeded |
| Metrics | `intelligent_vms_operational_refresh_ok` | Latest aggregate operational-state refresh succeeded |
| Camera | `intelligent_vms_camera_health{state=...}` | Camera count in online/degraded/offline/unknown/other |
| Camera | `intelligent_vms_camera_health_stale` | Health rows older than twice the configured heartbeat |
| Media | `intelligent_vms_media_paths_present` | Camera health samples that currently see a MediaMTX path |
| Media | `intelligent_vms_camera_transport_ready` | Cameras whose latest RTSP TCP probe succeeded |
| Recording | `intelligent_vms_recording_active` | Enabled continuous recording policies |
| Recording | `intelligent_vms_recording_gap_candidates` | Continuous recordings whose initialized durable segment deadline expired |
| Recording | `intelligent_vms_recording_health_untracked` | Active continuous policies without an initialized durable health deadline |
| Nodes | `intelligent_vms_nodes{state=...}` | Enabled infrastructure nodes by bounded state |
| Nodes | `intelligent_vms_node_authority{mode=...}` | Nodes by central/autonomous/fenced authority mode |
| Nodes | `intelligent_vms_node_stale` | Enabled nodes with stale heartbeats |
| Capacity | `intelligent_vms_node_saturated{role=...}` | Nodes at/above configured placement headroom |
| Capacity | `intelligent_vms_node_capacity_unmeasured{role=...}` | Nodes missing trustworthy capacity/load measurements |
| Placement | `intelligent_vms_placement_assignments{role,status}` | Active and unapplied generations |
| AI | `intelligent_vms_ai_policies_enabled` | Enabled AI policies; this is configuration state, not inference throughput |
| Delivery | `intelligent_vms_outbox_items{status=...}` | Transactional outbox counts |
| Delivery | `intelligent_vms_outbox_oldest_pending_age_seconds` | Age of oldest pending/retry message |

No camera ID, tenant ID, site ID, stream key, model name, or arbitrary database state is emitted as a Prometheus label.

## 2. Recording-gap detection

Recording-gap health is durable PostgreSQL state, not a ClickHouse scan performed on every scrape.

When continuous recording is enabled, the VMS creates/updates a per-camera recording-health row with a startup grace deadline. Every accepted segment-complete hook advances:

- latest segment ID and completion time;
- recording node/generation evidence;
- `gap_deadline_at`.

The deadline window is:

`max(OBSERVABILITY_RECORDING_GAP_MIN_SECONDS, segment_duration_seconds × OBSERVABILITY_RECORDING_GAP_SEGMENT_MULTIPLIER)`

Defaults are 120 seconds minimum and a 2.0 segment multiplier. The recording policy already bounds segment duration. Disabling recording clears the active gap deadline. Delayed older segment hooks never move completion health backwards.

`intelligent_vms_recording_gap_candidates` counts only active continuous policies whose initialized deadline expired. Missing or uninitialized recording-health state is exported separately as `intelligent_vms_recording_health_untracked`.

This separation is deliberate for upgrade safety: an existing deployment can have active recording policies before the new recording-health table has received its first segment-complete evidence. Those policies must not be reported immediately as confirmed gap candidates. The default Helm alert treats prolonged untracked health as a warning after 30 minutes, while expired initialized deadlines remain the critical continuity signal.

Both metrics are operational signals; incident review should inspect recorder/node/media evidence before concluding that footage was lost.

## 3. Helm observability resources

The chart always supports normal Prometheus annotation scraping through `prometheus.*`.

Grafana dashboard ConfigMap:

```yaml
observability:
  grafana:
    enabled: true
    datasourceUid: prometheus
    labels:
      grafana_dashboard: "1"
```

Prometheus Operator alert rules are opt-in because clusters without the `PrometheusRule` CRD must continue to render/apply the base chart:

```yaml
observability:
  prometheusRule:
    enabled: true
```

Thresholds are under `observability.prometheusRule.thresholds`. They are deployment policy, not measured capacity claims.

## 4. External exporter contract

The VMS does not invent Kafka, PostgreSQL, ClickHouse, MediaMTX packet-quality, AI-runtime, or host-disk telemetry when those components do not expose it through the control API.

Operators may provide a complete boolean PromQL expression for each external signal:

```yaml
observability:
  prometheusRule:
    externalSignals:
      kafka:
        expr: ""
      postgres:
        expr: ""
      clickhouse:
        expr: ""
      mediamtx:
        expr: ""
      ai:
        expr: ""
      disk:
        expr: ""
```

An empty expression renders no external alert. Populate these only after the chosen exporter/managed-service metric names are known and verified. This avoids hard-coding a vendor-specific exporter contract into the product.

For AI, the built-in metric reports policy enablement only. Queue depth, inference latency, accelerator use, dropped samples and model-specific performance require the actual AI runtime/exporter before alerts are enabled.

## 5. Default SLO objectives

These are software operating objectives and starting alert defaults, not statements of achieved production performance.

| Objective | Default target / condition | Measurement |
| --- | --- | --- |
| Control API availability | 5xx ratio stays below configured threshold | HTTP request counter over rolling Prometheus window |
| Control API latency | p95 stays below configured threshold | HTTP duration histogram |
| Metrics freshness | operational refresh remains successful | `intelligent_vms_operational_refresh_ok == 1` |
| Recording continuity | no expired initialized continuous-recording gap deadlines | `recording_gap_candidates == 0` |
| Recording health initialization | active recording health becomes tracked after startup/upgrade | `recording_health_untracked == 0` after grace |
| Event delivery freshness | no dead letters; oldest pending age below threshold | outbox status + age |
| Node control freshness | no stale enabled node heartbeat | `node_stale == 0` |
| Placement convergence | no sustained unapplied generations | placement assignment status |
| Capacity safety | no role at/above configured headroom and no unmeasured role | node saturated/unmeasured metrics |

The shipped alert defaults use a 1% five-minute 5xx threshold and 1-second five-minute p95 latency threshold. They are intentionally configurable. A deployment may tighten them only after workload evidence supports the objective.

Hardware-dependent saturation is evaluated only when the node advertises both a configured capacity and a measured load for the relevant role. Missing measurements increment `node_capacity_unmeasured`; they are never treated as zero load.

## 6. Alert triage

- **Control API / operational refresh**: check PostgreSQL reachability, migration level, control-api logs and readiness.
- **Camera health / media path**: distinguish camera transport failure from MediaMTX path absence.
- **Recording gap**: check recording assignment generation, recorder node authority, MediaMTX path, storage and recent segment hooks.
- **Recording health untracked**: during upgrade/startup allow the configured initialization grace, then check whether segment-complete hooks are arriving and whether policy health rows are being initialized.
- **Outbox backlog/dead letters**: check Kafka/downstream availability before requeueing poison messages.
- **Node stale/saturated/unmeasured**: check node-agent heartbeat, authority mode and measured capacity/load.
- **Placement unapplied**: check current node generation, reconciler errors and stale-owner cleanup obligations.
- **External exporter alerts**: use the runbook for the configured exporter/service; the VMS does not reinterpret unknown third-party metrics.

## 7. Release gate

Phase 9.2 software acceptance requires:

1. unit tests and migration validation;
2. `helm lint`;
3. normal central/regional chart render;
4. observability render with `PrometheusRule` enabled;
5. rendered Grafana JSON parses successfully;
6. both repository CI workflows pass;
7. independent Reviewer PASS.

Production-qualified SLO attainment still requires external camera/hardware/network/storage/load evidence under the master production program.
