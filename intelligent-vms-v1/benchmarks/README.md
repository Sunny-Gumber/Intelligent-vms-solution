# Phase 8 Benchmark Harness

This directory defines the evidence format used for hardware qualification.

## Rule

A capacity or hardware recommendation is valid only when it references one or more saved benchmark result files produced by the same schema version.

CI smoke results prove tooling correctness only. They are **not** hardware qualification.

## Result schema

- `result.schema.json`
- schema version: `phase8-benchmark-v1`

Each result records:

- exact Git commit SHA;
- capture time;
- OS/kernel/architecture/Python/container runtime;
- CPU model/core count;
- RAM;
- network interfaces;
- storage path/capacity;
- GPU inventory when present;
- workload config, warmup, duration;
- throughput and failure rate;
- p50/p95/p99 latency;
- CPU/RAM/NIC/disk/GPU resource summaries.

Missing GPU is represented as `gpu_measured=false`, not 0% utilization.

Qualification uses a stricter measured-field schema than this file schema. Storage, reconnect, hardware-matrix, and reproducibility repeats must include the required paths in `HARDWARE_QUALIFICATION.md`. A missing key, `{}`, or `[]` is `MISSING_MEASURED_FIELD:<path>`. A present null is `<path> is null`. A non-finite number, string, or boolean is `<path> is not a finite number`. A missing `failure_rate` does not qualify and is not treated as zero. Null max CPU frequency, temperature, frequency ratio, and NIC speed remain optional.

## Workload tools

### Event/control-plane ingest

```bash
python tools/phase8_event_benchmark.py \
  --api http://vms-control:8000 \
  --cameras 500 \
  --events 50000 \
  --concurrency 100 \
  --output-json benchmarks/results/events-500.json \
  --output-csv benchmarks/results/index.csv
```

Use an authorized token when production auth is enabled.

### TCP reconnect storm

Useful for RTSP/control endpoint connection churn:

```bash
python tools/phase8_reconnect_benchmark.py \
  --host 10.0.0.20 --port 8554 \
  --attempts 20000 --concurrency 200 \
  --output-json benchmarks/results/reconnect.json
```

### RTSP source/viewer workload manifest

Requires ffmpeg on load-generator hosts:

```bash
python tools/phase8_media_plan.py \
  --server rtsp://10.0.0.20:8554 \
  --sources 500 --viewers 100 \
  --width 1920 --height 1080 --fps 25 --bitrate-kbps 1024 \
  --output-json benchmarks/results/media-plan-500.json \
  --source-shell /tmp/start-sources.sh \
  --viewer-shell /tmp/start-viewers.sh
```

The generated manifest is a workload definition, not a benchmark result. Run sources/viewers on dedicated load-generator hosts and collect resource/result evidence separately.

### End-to-end recording growth

Run while the VMS recording workload is active and point it at an isolated benchmark recording root:

```bash
python tools/phase8_recording_benchmark.py \
  --path /recordings/benchmark \
  --duration 300 \
  --expected-streams 500 \
  --expected-bitrate-kbps 1024 \
  --output-json benchmarks/results/recording-500.json
```

This measures actual file/byte growth observed from the recording path. Use a dedicated benchmark directory so unrelated retention/cleanup activity does not contaminate the result.

### Storage write baseline

```bash
python tools/phase8_storage_benchmark.py \
  --path /mnt/recording-benchmark \
  --streams 32 --mib-per-stream 2048 --chunk-mib 1 \
  --output-json benchmarks/results/storage-32.json
```

Use `--fsync` when the target production write policy requires it. This measures storage write baseline only; it does not certify end-to-end VMS recording throughput.

## Scale progression

Do not jump directly to a 100K claim.

1. 10–50 camera functional reference
2. 500-camera site class
3. 2,000-camera site/cluster class
4. 10,000-camera regional logical/media split tests
5. 100,000 logical-channel control/event simulation

100K continuous media requires distributed load generators and target infrastructure; results from one CI runner or laptop cannot be extrapolated as certification.

## Reproducibility

For each test keep:

- result JSON;
- optional CSV index row;
- media-plan manifest if applicable;
- host inventory;
- exact command;
- warmup and duration;
- external conditions such as storage RAID/object tier, NIC/switch path, and GPU power/thermal limits.

Repeat important qualification runs at least three times in Phase 8 QA and report variance.

## Hardware qualification matrix

After repeated real benchmark runs exist, compile them into deployment profiles with:

```bash
python tools/phase8_hardware_matrix.py \
  --results benchmarks/results/ \
  --reproducibility-report benchmarks/results/reproducibility.json \
  --demand /tmp/project-demand.json \
  --output benchmarks/results/hardware-matrix.json
```

Read `HARDWARE_QUALIFICATION.md` before interpreting the output. Missing evidence is deliberately reported as UNQUALIFIED.

Before hardware qualification, validate repeatability with `BENCHMARK_REPRODUCIBILITY.md` and `tools/phase8_reproducibility.py`.
