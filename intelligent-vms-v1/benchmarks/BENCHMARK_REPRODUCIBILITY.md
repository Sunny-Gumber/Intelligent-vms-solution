# Phase 8 Benchmark Reproducibility QA

Issue: #188

## Purpose

A single fast benchmark run is not hardware evidence. This gate decides whether repeated benchmark results are comparable and stable enough to be eligible for hardware qualification.

## Group identity

Runs are compared only when all of these match:

- software commit SHA;
- hardware fingerprint;
- workload type;
- complete workload configuration.

Changing concurrency, bitrate, resolution, FPS, stream count, storage policy, model precision or other workload configuration creates a different QA group.

## Default QA policy

- at least 3 repeats;
- each measured duration >= 300 seconds;
- each warmup >= 30 seconds;
- maximum failure rate <= 0.1%;
- qualification-capacity coefficient of variation <= 10%;
- qualification-capacity relative range <= 20%;
- p95 latency coefficient of variation <= 15% when latency is available;
- latency percentiles must be ordered p50 <= p95 <= p99 <= max;
- any observed CPU thermal sensor high/critical limit is a hard failure.

The qualification-capacity dimension is workload specific. Examples:

- event ingest: events/s;
- recording: observed recording Mbps;
- storage: storage write Mbps;
- media relay: media Mbps;
- AI inference: MPix/s.

## Required measured fields

A repeat counts only when its required measured fields are present and finite. The schema is `required_measured_fields_for_workload` in `tools/phase8_benchmark_common.py`, the same function the hardware matrix uses. See `HARDWARE_QUALIFICATION.md` for the storage, reconnect, hardware-matrix, and reproducibility lists.

Each required path uses one reason, and that reason names the full path. A missing key, `{}`, `[]`, or any other object or list fails the group with `MISSING_MEASURED_FIELD:<path>` and `repeat_count` 0. A present null remains `<path> is null`. A non-finite number, string, or boolean remains `<path> is not a finite number`. A boolean is not stored as 0 or 1. A missing `failure_rate`, including `{}` or `[]`, stays unset. It is not stored as `0.0`, and the reason is `MISSING_MEASURED_FIELD:result.failure_rate`.

Duplicate JSON keys are rejected with `duplicate JSON key: <path>` before last-wins can keep a trailing value. A mapped capacity of zero or negative zero fails the group because that metric is not a positive finite number. Reconnect has no capacity row, so a complete reconnect repeat can still pass with the undefined-metric warning. Nesting deeper than 32 fails closed and the reason names the path.

Optional sensor nulls stay accepted: max CPU frequency, platform temperature, CPU frequency ratio, and NIC `speed_mbps`. Those nulls do not split a content fingerprint and do not, by themselves, reject the repeat.

## Thermal and frequency awareness

Benchmark sampling records CPU frequency and platform thermal sensors where the OS exposes them. NVIDIA GPU samples include GPU temperature where `nvidia-smi` supports it.

Missing thermal sensors are never interpreted as a cool system. They produce a warning. For target production hardware, use `--require-thermal` when sensor telemetry is expected; this converts missing thermal evidence into a QA failure.

A CPU frequency/max ratio below 0.50 is surfaced as a warning for operator review. Low frequency can be DVFS/power policy rather than thermal throttling, so it is not automatically classified as thermal throttling without corresponding sensor evidence.

## Run

```bash
python tools/phase8_reproducibility.py \
  --results benchmarks/results/ \
  --output benchmarks/results/reproducibility.json \
  --fail-on-rejected-groups
```

For a production hardware qualification run with usable thermal sensors:

```bash
python tools/phase8_reproducibility.py \
  --results benchmarks/results/ \
  --output benchmarks/results/reproducibility.json \
  --require-thermal \
  --fail-on-rejected-groups
```

## Hardware matrix handoff

The hardware matrix CLI requires the reproducibility report and considers only benchmark IDs belonging to PASS groups. `build_matrix` also applies that group verdict itself, including on a direct call and when approved fingerprints are supplied. The default limits match this policy: capacity CV <= 10%, capacity relative range <= 20%, and p95 latency CV <= 15%. A failed group cannot become `QUALIFIED_FROM_MEASURED_EVIDENCE`.

```bash
python tools/phase8_hardware_matrix.py \
  --results benchmarks/results/ \
  --reproducibility-report benchmarks/results/reproducibility.json \
  --demand /tmp/project-demand.json \
  --output benchmarks/results/hardware-matrix.json
```

Rejected or unrelated benchmark IDs are listed in the matrix as QA-excluded and cannot influence node sizing.

## Interpretation

`PASS` means the repeated software/hardware/workload evidence is reproducible under the configured QA policy. It does not by itself prove a deployment scale that was not actually exercised.

Phase 8 still requires the target workload levels and real hardware/environment runs before any 500/2K/10K/100K profile can be called qualified.
