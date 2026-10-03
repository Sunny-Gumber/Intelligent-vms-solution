# ADR-0002: Kafka-compatible event backbone

## Decision
Use a partitioned Kafka-compatible log for normalized camera, AI, health and integration events.

## Partition key
`tenant_id + camera_id` by default.

## Reason
This maintains per-camera ordering while allowing consumer groups to scale processing and independent subscribers to receive the same event stream.
