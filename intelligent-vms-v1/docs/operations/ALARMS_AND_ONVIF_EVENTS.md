# ONVIF Events, Alarms & Diagnostics Operations

## Services

- `onvif-event-worker`: maintains camera PullPoint subscriptions and publishes normalized events.
- `alarm-worker`: consumes normalized events and opens deduplicated alarm instances.
- `control-api`: manages rules, alarm acknowledgement/close and current diagnostics.

## Alarm APIs

- `POST /api/v1/alarms/rules`
- `GET /api/v1/alarms/rules`
- `PATCH /api/v1/alarms/rules/{id}`
- `DELETE /api/v1/alarms/rules/{id}`
- `GET /api/v1/alarms`
- `POST /api/v1/alarms/{id}/acknowledge`
- `POST /api/v1/alarms/{id}/close`

Example rule:

```json
{
  "tenant_id": "default",
  "site_id": "warehouse-01",
  "name": "Intrusion alarm",
  "event_types": ["intrusion", "tripwire"],
  "alarm_severity": "high",
  "cooldown_seconds": 60
}
```

## Diagnostics

`GET /api/v1/diagnostics/cameras/{camera_id}`

The first sample returns byte counters but Mbps may be null. A later sample from the same API process can derive the short-interval byte rate. Use Prometheus for production time-series.

## Troubleshooting PullPoint

1. confirm the camera capability snapshot reports Events service;
2. inspect event-worker logs for subscription creation/retry;
3. verify the returned PullPoint address remains inside allowed camera networks;
4. check camera clock and ONVIF credentials;
5. test vendor firmware with ONVIF Device Test Tool where applicable;
6. inspect Kafka/event-writer health.

Never log PullPoint SOAP envelopes with WSSE credentials in production.
