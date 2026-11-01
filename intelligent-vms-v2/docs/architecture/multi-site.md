# Multi-Site and Federation

## Implemented in 0.1.0-alpha

- Organization hierarchy: organization → site → building → floor, stored with
  a materialized path so ancestor queries are a single `LIKE`.
- Site-scoped authorization, including ancestor resolution. A user scoped to a
  building sees its cameras; a user scoped to nothing sees nothing (fails
  closed).
- Remote node registration with an encrypted bearer token, TLS verification
  enforced.
- Health and inventory aggregation: each registered node is polled for camera
  counts, online counts and open alarms, and the results are presented
  centrally.

## NOT implemented

| Capability | Why not |
| --- | --- |
| Distributed recording | Requires the fencing design in `ha-design.md` |
| Cross-node failover | Same |
| Live video proxying between nodes | Requires media routing and a bandwidth policy; the gateway is deliberately not in the control plane's path |
| Distributed control plane | Requires consensus for configuration writes |

These return `501 not_implemented` from
`app/federation/aggregator.py`, rather than doing something partial.

## Intended growth path

1. **Single server** (today). One control API, one recorder, one database.
2. **Multiple recorders, one control plane.** Already supported by the claim
   and heartbeat model; needs operational documentation and a load test.
3. **Federated sites.** Each site runs a full stack; a central instance
   aggregates. Video stays local — this is both a bandwidth and a data
   residency decision.
4. **Distributed recording.** Only after the HA acceptance criteria pass.

## Bandwidth posture

Federation moves **metadata**, never video, by design. A 50-site deployment
aggregates counts and alarms over a few kilobits per second. An operator who
needs to watch a remote camera connects to that site's gateway directly, so
the WAN carries one stream for one viewer rather than every stream
continuously.
