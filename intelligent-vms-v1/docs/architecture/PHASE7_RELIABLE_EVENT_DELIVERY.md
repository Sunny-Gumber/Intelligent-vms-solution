# Phase 7 — Reliable Event Delivery / Transactional Outbox

## Goal

Remove the failure window where durable control-state changes commit successfully but their corresponding Kafka event is lost.

The outbox now covers camera-health transitions, public/internal normalized event ingest, central-mode ONVIF events, AI result events, and completed recording metadata.

## Transaction boundary

For a health transition:

1. lock/update camera_health_state;
2. create the normalized event;
3. insert an event_outbox row in the same PostgreSQL transaction;
4. commit once.

If the transaction rolls back, neither the state transition nor outbox message exists. If it commits, the event remains durable even while Kafka is unavailable.

## Multi-replica dispatch

Workers claim bounded batches with PostgreSQL FOR UPDATE SKIP LOCKED. A short claim lease is persisted before broker I/O. Publishing happens outside the claim transaction, and delivered/retry state is updated only when the claim token still matches.

## Crash after publish

Kafka may acknowledge a message and the worker may crash before marking the row delivered. After the claim lease expires another worker republishes the same message ID. This is intentional at-least-once behavior.

Replay tolerance:
- alarm worker uses deterministic dedupe keys protected by a unique DB constraint;
- recording segments use deterministic segment IDs;
- ClickHouse event ingestion uses ReplacingMergeTree and collapses duplicate IDs inside each consumed batch.

## Broker outage behavior

Broker/network outages are infrastructure failures and do not consume a finite poison-message DLQ budget. They retry indefinitely with bounded exponential backoff. Poison/serialization failures may reach dead after OUTBOX_MAX_ATTEMPTS.

Kafka connectivity is lazy, so broker outage does not prevent control-api startup. DB-backed operations continue and durable outbox rows accumulate.

## Bounded operation

Important controls include OUTBOX_BATCH_SIZE, OUTBOX_CLAIM_SECONDS, OUTBOX_POLL_INTERVAL_SECONDS, OUTBOX_BACKOFF_MAX_SECONDS, cleanup batch size, and delivered/dead retention. No worker performs an unbounded backlog scan.

## Operator repair

GET /api/v1/system/outbox returns pending/retry/dead/delivered counts.

POST /api/v1/system/outbox/{message_id}/requeue requeues one dead item and preserves its identity/payload.

## Ingest and replay boundary

Public/internal normalized event ingest is idempotent by deterministic event ID and returns accepted once PostgreSQL has durable custody. Central-mode ONVIF and AI result events use the same path. Recording metadata uses deterministic segment IDs.

Regional ONVIF still writes first to the Step 1C-C regional spool during WAN operation; after reconnect, the central internal ingest transfers custody to PostgreSQL before the regional spool deletes its local item.

The Kafka boundary is therefore asynchronous for these normalized event/metadata paths. At-least-once broker replay remains expected and downstream sinks must remain idempotent.