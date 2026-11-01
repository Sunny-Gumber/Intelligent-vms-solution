# ADR-0012 — Analytics results always record their origin

**Status:** Accepted

## Context

This is the single easiest place for a VMS to mislead its buyer. A product can
receive ONVIF motion events from a camera and present them as "AI video
analytics". The camera did the work; the VMS stored a row.

The requirements say so directly: *"Do not claim server-side inference exists
merely because camera events can be received."*

## Decision

Every analytics record carries a mandatory `source`:

| Value | Meaning |
| --- | --- |
| `camera_edge` | The CAMERA detected this. Intelligent VMS only stored it. |
| `server_inference` | THIS SOFTWARE ran a model on decoded frames. |
| `external` | A third-party engine pushed it over the API. |

The field is non-nullable in the database, present in the DTO, returned by
search, and broken out in `/reports/analytics-summary`. `GET
/analytics/capabilities` reports `models_bundled: false`,
`matching_engine_implemented: false` and `server_ocr_implemented: false`.

A unit test asserts that an ONVIF motion notification maps to `camera_edge`,
so the distinction cannot regress silently.

## Consequences

**Costs:** the product's analytics claims look smaller than competitors who
blur this line. That is the intent. What IS genuinely implemented — zone rules
(intrusion, tripwire, loitering), counting aggregation, metadata smart search,
plate normalization and watchlist matching, and the privacy/audit controls —
works on detections from **any** source, which is why those are marked
implemented while the detectors are marked designed.
