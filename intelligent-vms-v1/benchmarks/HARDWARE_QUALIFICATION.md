# Phase 8 Hardware Qualification Matrix

Issue: #187

## Principle

The matrix compiler is deliberately conservative. It never derives a server capacity from formulas alone.

A role is qualified only when the repository has repeated benchmark result files from the same software commit and hardware fingerprint that pass the configured duration, warmup, failure-rate, CPU and RAM limits.

Default evidence policy:

- at least 3 qualified repeats;
- at least 300 seconds measured duration per run;
- at least 30 seconds warmup;
- failure rate <= 0.1%;
- CPU p95 <= 70%;
- RAM p95 <= 75%;
- use the minimum observed capacity across qualified repeats;
- apply 80% design headroom to that conservative observed capacity;
- add N+1 to the resulting node count.

These are qualification-policy defaults, not claims about a particular server.

## Demand is an explicit input

`deployment-demand.template.json` intentionally contains null demand fields.

The project must provide workload demand for each target profile. The tool does not assume bitrate, events per camera, AI FPS, viewer count, or retention policy.

Target profile placeholders:

- 500 cameras;
- 2,000 cameras;
- 10,000 cameras;
- 100,000 logical channels.

## Supported measured evidence

| Benchmark workload | Qualified role | Capacity dimension |
|---|---|---|
| event-ingest-http | event_ingest | events_per_second |
| recording-directory-growth | recording | recording_mbps |
| synthetic-storage-write | storage | storage_write_mbps |
| control-api | control_api | control_ops_per_second |
| media-relay | media | media_mbps |
| ai-inference | ai | ai_mpix_s |

`media-relay`, `control-api`, and `ai-inference` require dedicated measured benchmark results. Until those result types exist, those roles remain UNQUALIFIED.

Synthetic storage evidence qualifies the storage baseline role only. It does not qualify VMS recording by itself.

## Reproducibility prerequisite

The CLI requires a `phase8-reproducibility-v1` report. Only benchmark IDs from PASS groups are eligible for hardware qualification. Run `phase8_reproducibility.py` first.

## Usage

Copy the demand template and fill only project-approved demand inputs:

```bash
cp benchmarks/deployment-demand.template.json /tmp/project-demand.json
```

Then run:

```bash
python tools/phase8_hardware_matrix.py \
  --results benchmarks/results/ \
  --reproducibility-report benchmarks/results/reproducibility.json \
  --demand /tmp/project-demand.json \
  --output benchmarks/results/hardware-matrix.json
```

## Output statuses

- `QUALIFIED_FROM_MEASURED_EVIDENCE`: sufficient repeated evidence exists.
- `UNQUALIFIED`: evidence or demand is missing/rejected.
- `NOT_REQUIRED`: explicit deployment demand is zero.

Every qualified row includes benchmark IDs, commit SHA, hardware fingerprint, conservative observed capacity, design-safe capacity, base node count and N+1 result.

## Evidence isolation rules

- Runs from different commits do not combine into one repeat set.
- Runs from different hardware fingerprints do not combine.
- Short CI smoke results do not satisfy the default duration/warmup policy.
- High failure rate or over-threshold CPU/RAM evidence is rejected.
- Missing measurements remain missing; they are never converted to zero utilization.
- A missing `failure_rate` is unqualified. It is not stored as `0.0`.
- A rejected record does not store `failure_rate` as `0.0`. The internal row uses none.
- Identity flags (`thermal_measured`, `thermal_limit_exceeded`, `gpu_measured`, NIC `is_up`, and `fsync_each_chunk`) are not coerced to numeric 0 or 1 and are not capacity.
- Duplicate JSON keys are rejected. The rejection names the path, for example `duplicate JSON key: result.failure_rate`.
- Nesting deeper than 32 names the path of the container that crosses the limit.
- A direct `build_matrix` call does not qualify a record that failed the reproducibility percentile-order or thermal-limit check.
- A direct `build_matrix` call applies the reproducibility group verdict to every supplied result in the same commit, hardware, and workload group before approved fingerprints can drop a member. Capacity coefficient of variation above 10%, capacity relative range above 20%, and p95 latency coefficient of variation above 15% stay `UNQUALIFIED`. Omitting the dispersed members from the fingerprint map does not qualify the rest.
- The matrix CLI also requires the reproducibility report to name every result in that group. A report built from only a subset stays `UNQUALIFIED`.

## Required measured fields

`QUALIFIED_FROM_MEASURED_EVIDENCE` requires every repeat to satisfy the measured-field schema for its workload. The lists live in `tools/phase8_benchmark_common.py` (`required_measured_fields` and `required_measured_fields_for_workload`). The hardware matrix and the reproducibility gate both use `missing_measured_field_reasons`, which calls `required_measured_fields_for_workload`. They do not keep a second copy of the path list.

Every required path uses one classification. The reason always names the full path:

- a missing key, `{}`, `[]`, or any other object or list is `UNQUALIFIED` with `MISSING_MEASURED_FIELD:<path>`;
- a present JSON null is unqualified with `<path> is null`;
- a non-finite number (`NaN`, infinity), or a non-numeric scalar such as a string or boolean, is rejected with `<path> is not a finite number`.

A boolean is not coerced to 0 or 1 on a required path. A missing `failure_rate`, including `{}` or `[]`, stays unset. It is not stored as `0.0`, and its reason is `MISSING_MEASURED_FIELD:result.failure_rate`.

Shared core for hardware-matrix and reproducibility repeats, and for the reconnect driver (`tcp-reconnect-storm`):

- `result.operations_ok`, `result.operations_failed`, `result.failure_rate`, `result.throughput_ops_s`
- `result.latency.count`, `result.latency.p50_ms`, `result.latency.p95_ms`, `result.latency.p99_ms`, `result.latency.max_ms`, `result.latency.mean_ms`
- `resources.samples`
- `resources.cpu_pct`, `resources.cpu_freq_mhz`, `resources.ram_used_bytes`, `resources.ram_pct`, `resources.net_rx_mbps`, `resources.net_tx_mbps`, `resources.disk_read_mbps`, `resources.disk_write_mbps`, each with `mean`, `p95`, and `max`

Storage evidence (`synthetic-storage-write`) adds `result.bytes_written`, `result.aggregate_write_mbps`, and `result.aggregate_write_MBps`.

Mapped workloads add their capacity field when it is not already in the core: `result.observed_recording_mbps`, `result.observed_media_mbps`, or `result.observed_ai_mpix_s`. Event ingest and control-api use `result.throughput_ops_s`, which is already required.

These fields are the numbers the current drivers write. They are not a capacity claim.

Optional sensor nulls stay accepted. A null, a missing key, or a number in these fields does not by itself make a repeat unqualified:

- `resources.cpu_freq_max_mhz.mean`, `resources.cpu_freq_max_mhz.p95`, `resources.cpu_freq_max_mhz.max`
- `resources.max_temperature_c.mean`, `resources.max_temperature_c.p95`, `resources.max_temperature_c.max`
- `resources.cpu_freq_ratio_min`
- `environment.hardware.network_interfaces[].speed_mbps`

Reconnect evidence has no capacity mapping. A complete reconnect repeat can pass reproducibility and still leave every capacity role `UNQUALIFIED`.

A mapped capacity of zero or negative zero fails reproducibility. The undefined-metric warning is only for a workload that has no capacity row.

### Exported required paths

This inventory is the set returned by `required_measured_fields` and `required_measured_fields_for_workload`. A test locks the backtick paths in this section to that set.

- `result.aggregate_write_MBps`
- `result.aggregate_write_mbps`
- `result.bytes_written`
- `result.failure_rate`
- `result.latency.count`
- `result.latency.max_ms`
- `result.latency.mean_ms`
- `result.latency.p50_ms`
- `result.latency.p95_ms`
- `result.latency.p99_ms`
- `result.observed_ai_mpix_s`
- `result.observed_media_mbps`
- `result.observed_recording_mbps`
- `result.operations_failed`
- `result.operations_ok`
- `result.throughput_ops_s`
- `resources.cpu_freq_mhz.max`
- `resources.cpu_freq_mhz.mean`
- `resources.cpu_freq_mhz.p95`
- `resources.cpu_pct.max`
- `resources.cpu_pct.mean`
- `resources.cpu_pct.p95`
- `resources.disk_read_mbps.max`
- `resources.disk_read_mbps.mean`
- `resources.disk_read_mbps.p95`
- `resources.disk_write_mbps.max`
- `resources.disk_write_mbps.mean`
- `resources.disk_write_mbps.p95`
- `resources.net_rx_mbps.max`
- `resources.net_rx_mbps.mean`
- `resources.net_rx_mbps.p95`
- `resources.net_tx_mbps.max`
- `resources.net_tx_mbps.mean`
- `resources.net_tx_mbps.p95`
- `resources.ram_pct.max`
- `resources.ram_pct.mean`
- `resources.ram_pct.p95`
- `resources.ram_used_bytes.max`
- `resources.ram_used_bytes.mean`
- `resources.ram_used_bytes.p95`
- `resources.samples`

## Prohibited interpretation

A 100K row that is `UNQUALIFIED` is not a failed product; it means the required benchmark evidence has not yet been produced.

Never replace UNQUALIFIED with a spreadsheet estimate and call it hardware certification.
