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

The hardware matrix CLI requires the reproducibility report and considers only benchmark IDs belonging to PASS groups:

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
