# Integration architecture

How third-party systems connect to Intelligent VMS. Everything here is built on
three primitives, so one integration pattern covers access control, alarm
management, building management and analytics vendors alike.

| Primitive | Direction | Use it for |
| --- | --- | --- |
| REST API (`/api/v1`) | inbound | configuration, queries, commands |
| Inbound event API | inbound | "something happened in my system" |
| Outbound webhooks | outbound | "something happened in the VMS" |
| WebSocket stream | outbound | live push to an operator UI |

## Authentication for integrations

Use an **API key**, not a user password.

```http
POST /api/v1/apikeys
Authorization: Bearer <admin access token>

{ "name": "access-control-bridge",
  "permissions": ["live.view", "playback.view", "rule.manage"],
  "expires_days": 365 }
```

The response contains the key **once**. Afterwards only a keyed hash is stored.

Two rules are enforced in code and cannot be bypassed:

1. **A key can never exceed its creator's permissions.** Requesting
   `user.manage` from an operator account is rejected with
   `permission_escalation`.
2. **A key inherits its owner's site and camera scope.** Deactivating the owner
   immediately disables the key.

Send it as `X-API-Key: <prefix>.<secret>`.

## Pattern 1 - access control system raises an event

A door controller reports a forced-door. You want the VMS to record, raise an
alarm and bookmark the footage.

```http
POST /api/v1/events
X-API-Key: ...

{ "type": "camera.tamper",
  "severity": "high",
  "camera_id": "7f3c...",
  "occurred_at": "2026-10-20T09:14:02Z",
  "payload": { "door": "North entrance", "badge": "unknown", "source_system": "ACS" } }
```

Then create a rule once, in the VMS, that turns such events into action:

```http
POST /api/v1/rules

{ "name": "Forced door",
  "event_types": ["camera.tamper"],
  "conditions": { "field": "source_system", "op": "eq", "value": "ACS" },
  "severity": "high",
  "raise_alarm": true,
  "start_recording": true,
  "record_seconds": 120,
  "capture_snapshot": true,
  "notify_channels": ["security-webhook"] }
```

The condition language is deliberately closed - `eq`, `ne`, `gt`, `gte`, `lt`,
`lte`, `in`, `contains`, grouped by `all`/`any`. There is no expression
evaluation, so a rule can never execute arbitrary logic.

## Pattern 2 - VMS notifies an external system

Register a webhook channel, then reference it from a rule's `notify_channels`.

Every delivery carries:

```
X-VMS-Event:     <event id>
X-VMS-Timestamp: <unix seconds>
X-VMS-Signature: sha256=<hex hmac of "timestamp.body">
```

Verify like this (pseudocode):

```python
expected = hmac_sha256(secret, f"{timestamp}.".encode() + raw_body)
if not constant_time_equals(expected, received_signature):  reject()
if abs(now - timestamp) > 300:                              reject()   # replay
```

The timestamp is **inside** the signed material, which is what makes captured
requests non-replayable. Delivery is retried with bounded exponential backoff;
4xx responses other than 429 are treated as permanent and not retried.

## Pattern 3 - external analytics engine

A third-party detector pushes results in. The VMS stores them, links them to
recorded footage and makes them searchable - and permanently records that they
came from outside:

```http
POST /api/v1/analytics/detections

{ "camera_id": "7f3c...",
  "object_class": "person",
  "confidence": 0.91,
  "bbox": [0.31, 0.44, 0.08, 0.22],
  "track_id": "ext-8841",
  "model_name": "partner-detector", "model_version": "3.2" }
```

The stored `source` is `external`. It is never relabelled as
`server_inference`. Searches and reports always expose this field, so an
investigator can see what produced a detection.

Bounding boxes are normalized `[x, y, w, h]` in 0..1, so they survive stream
profile and resolution changes.

## Pattern 4 - live operator push

```
wss://vms.example.com/api/v1/events/stream?token=<access token>
```

Messages are `{"type": "event", "data": {...}}` with periodic
`{"type": "heartbeat"}`. The stream is **scope-filtered server-side**: a user
restricted to one site never receives another site's events, even if they craft
their own client.

## Building management systems

BMS integrations use Pattern 1 inbound (HVAC fault, fire panel, lift alarm) and
Pattern 2 outbound (camera offline, storage critical). No BMS-specific
connector ships with this release; the mapping is a small adapter on the BMS
side, and the contract above is stable.

## What is NOT provided

Stated plainly so nobody plans around something that does not exist:

- **No vendor-specific connectors.** No Lenel, Genetec, Honeywell, Siemens or
  similar adapters ship with this release.
- **No ONVIF Profile G server.** Intelligent VMS is an ONVIF *client*; it does
  not expose its own recordings over ONVIF to another VMS.
- **No RTSP re-streaming to third parties.** Live media is served as WebRTC/HLS
  through the media gateway to authorized clients only.
- **No SDK packages.** The REST contract (`contracts/openapi.json`) is the
  integration surface; generate a client from it with your preferred generator.

## Rate limits and versioning

- Limits are per API key (`rate_limit_per_minute`, default 600). Exceeding one
  returns `429` with `retry_after_s`.
  **Caveat:** limits are enforced per API worker process. See
  `docs/LIMITATIONS.md`.
- The API is versioned by path. Within `v1`, fields and enum members are only
  ever **added**. Breaking changes require `/api/v2`.
- Branch on the stable `code` field of an error response, never on `detail`
  text.
