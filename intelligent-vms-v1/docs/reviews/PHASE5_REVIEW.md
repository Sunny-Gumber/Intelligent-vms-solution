# Phase 5 Independent Review — ONVIF Events, Alarms & Diagnostics

Reviewer status: **PASS for merge as a development/production-candidate capability.**

## Blocker found and fixed

### Invalid event SOAP namespace declarations — FIXED
Initial PullPoint implementation generated bodies/headers using `tev:`, `wsa:` and `wsnt:` prefixes while the common ONVIF SOAP envelope declared only device/media/schema namespaces. A real ONVIF camera could reject the resulting XML.

Fix:
- common envelope now declares ONVIF Events, WS-Addressing and WS-Notification namespaces;
- QA added request-generation parsing tests for CreatePullPointSubscription and Unsubscribe;
- dedicated VMS CI and full repository CI passed after the change.

## Security/reliability positives

- PullPoint/Event/Subscription XAddrs inherit the existing network allowlist.
- SOAP parsing remains bounded and uses defusedxml.
- PullPoint long polls use explicit timeout headroom.
- exponential backoff reduces reconnect storms.
- deterministic event IDs make re-delivery safe for search/event persistence.
- alarm cooldown dedupe is protected by a database unique constraint.
- alarm APIs enforce role + tenant/site scope.
- Initialized synchronization events remain searchable but cannot open alarms.
- diagnostics are scoped through authorized camera access.
- credentials are decrypted only inside regional event workers and are not returned via APIs.

## Accepted follow-ups for later phases

### Guaranteed event delivery
Health/event state transitions currently publish directly to Kafka. A transactional outbox is required before final production certification where guaranteed delivery across DB/Kafka failure boundaries is required.

### Subscription ownership at 100K+
Current worker ownership supports media-node filtering plus deterministic shards. Phase 7 must replace configuration-only ownership with region/node leases/fencing and eliminate fixed-scan assumptions.

### Diagnostic history
Current API rate calculation is per-process and short-interval. Prometheus/Grafana must be the HA historical source in Phase 9.

### Real-device interoperability
CI validates protocol construction/parsing, but final production certification still requires a real-camera matrix across target vendors/firmware.

## CI evidence

After Reviewer fix:
- Intelligent VMS CI: PASS
- Full CamVault CI: PASS

## Decision

No remaining Phase-5 BLOCKER. Merge is approved. Production-wide certification remains governed by Phases 7–9 and external hardware/camera qualification.
