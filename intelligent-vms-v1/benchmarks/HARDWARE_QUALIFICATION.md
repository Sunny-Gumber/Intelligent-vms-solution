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

## Prohibited interpretation

A 100K row that is `UNQUALIFIED` is not a failed product; it means the required benchmark evidence has not yet been produced.

Never replace UNQUALIFIED with a spreadsheet estimate and call it hardware certification.
