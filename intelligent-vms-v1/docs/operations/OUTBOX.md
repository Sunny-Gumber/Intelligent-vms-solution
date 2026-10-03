# Transactional Outbox Operations

## Normal state

Use GET /api/v1/system/outbox. Healthy operation should have low pending/retry counts and normally zero dead rows.

## Kafka outage

Expected behavior:
- control API remains available;
- health-state DB transitions continue;
- transition events accumulate durably;
- broker failures retry with bounded exponential backoff;
- valid events do not move to DLQ merely because the outage is long;
- after recovery, workers drain bounded batches.

Do not delete pending or retry rows during a broker outage.

## Worker crash

Claims expire after OUTBOX_CLAIM_SECONDS. A crash after Kafka publish but before delivered-marking can cause replay. Consumers must tolerate the same message ID more than once.

## Dead letters

Dead means a poison/non-transient delivery exhausted its configured attempt budget, not simply that Kafka was offline.

Inspect last_error, correct the root cause, then call POST /api/v1/system/outbox/{message_id}/requeue.

## Backlog tuning

Tune batch size, poll interval, claim duration and backoff only from measured Phase 8 evidence. Do not infer production values from laptop tests.

## Retention

Delivered rows are retained briefly for diagnostics and cleaned in bounded batches. Dead rows are retained longer for repair/audit and cleaned by age.

## Durable ingest coverage

The central outbox accepts:
- camera health transition events;
- public/internal normalized events;
- central-mode ONVIF events;
- AI result events;
- recording segment metadata.

Event and segment IDs are deterministic. Duplicate retries are accepted idempotently without creating a second outbox row.
