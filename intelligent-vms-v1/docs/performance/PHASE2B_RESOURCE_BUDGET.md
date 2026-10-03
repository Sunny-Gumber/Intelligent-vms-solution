# Phase 2B control/reconciler resource budget

The hardening services must not become an O(camera_count) central bottleneck.

## Authentication

JWKS signing keys are cached by the JWT library. Budget:
- no network JWKS fetch on normal cached-token verification;
- token verification CPU tracked separately from API business logic;
- authorization is O(1) for tenant plus O(site claim count) membership;
- do not carry enormous per-camera ACL lists in JWTs.

Scale trigger:
- p95 auth verification latency > 5 ms sustained on control nodes;
- identity/JWKS error rate;
- CPU > 65% caused by crypto validation.

At higher rates, increase stateless API replicas; do not weaken signature verification.

## Reconciler

Development defaults:
- interval: 30 s
- scan batch: 500 cameras/run
- change budget: 200 path adds/run

These are safety defaults, not 100K production throughput targets.

The reconciler makes one path-list call per run plus bounded DB/read and add calls. It must never create one HTTP request per healthy camera simply to prove health.

## Reconnect storms

After media-node restart, desired paths may all be absent. Recovery is intentionally rate-limited.

Production regional controller sizing should define:
- paths/second safely accepted by each MediaMTX node;
- camera RTSP reconnect rate tolerated by access switches/cameras;
- stagger/jitter across media nodes;
- N+1 node capacity.

Scale-up is complete only when restore time under a full-node restart meets the deployment recovery objective without overwhelming cameras/network.

## Database

Current development query reads a bounded batch. Before 10K+ regional assignment, replace fixed first-batch scanning with cursor/partition ownership so every camera receives periodic reconciliation.

PostgreSQL advisory lock protects one logical run; regional scale moves to per-node queue/lease ownership rather than one global lock.

## RAM/CPU effect

Auth and reconciliation should be small relative to media. Benchmark separately:
- JWT verifies/s/core
- reconciler RSS
- DB rows scanned/s
- path add latency
- control API p95 under active reconciliation

Boss rule: no increase in media channel count is accepted unless control-plane p95 and reconnect recovery stay inside measured budgets.
