# Phase 5 CPU / RAM / Socket / Event Budget

## Principle

Persistent ONVIF subscriptions are primarily a socket/RAM/concurrency problem; event bursts and rule evaluation are CPU/DB problems. They are sized separately.

Run:

```bash
python tools/phase5_sizing.py \
  --cameras 100000 \
  --event-capable-fraction 1 \
  --subscriptions-per-worker 200 \
  --events-per-camera-hour 2 \
  --avg-candidate-rules 3
```

At those assumptions the calculator reports structural counts only. It does not claim that 200 is optimal for a particular server.

## Required measurements per target platform

ONVIF worker:
- idle RSS;
- incremental bytes/subscription;
- file descriptors/subscription;
- TLS/non-TLS CPU;
- PullMessages responses/s/core during event storms;
- reconnect recovery rate.

Alarm worker:
- candidate rule checks/s/core;
- Kafka lag under burst;
- PostgreSQL alarm inserts/s and p95 latency;
- cache rebuild cost with 1K/10K/100K rules.

Diagnostics:
- MediaMTX metrics payload size at chosen path density;
- parse time/core;
- scrape interval/cardinality;
- Prometheus retention/storage.

## Production guardrails

- keep subscription density low enough that losing one worker does not reconnect an excessive camera population at once;
- jitter reconnect backoff;
- reserve CPU/RAM/socket headroom;
- configure OS file descriptor limits explicitly;
- shard by media node/region;
- use Prometheus for high-cardinality time-series rather than PostgreSQL.
