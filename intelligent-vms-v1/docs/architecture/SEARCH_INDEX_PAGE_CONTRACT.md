# Search and index page contract

Event search and the recording index use one contract when a backend row cannot
be parsed. The HTTP status stays successful. Existing response fields stay in
place. A malformed row is never copied into the response, the logs, or metrics.

This is the partial-page contract, not a backend error. A 502 would turn one
bad historical row into a failed page and break clients that already render a
successful event list, history page, or recording timeline. Those clients keep
working: JSON arrays stay arrays, and `EventHistoryPage` only adds fields.

## Page fields

Paged object responses (`GET /api/v1/events/history`) include:

- `items`: valid rows for this page, in the existing order.
- `next_before` and `next_before_id`: set only when another valid event exists
  before this page. Clients stop when both are empty.
- `partial`: `true` when this page skipped at least one malformed row.
- `skipped_rows`: how many malformed rows were skipped while filling this page.

`partial` does not mean more valid rows remain. A short `items` list with an
empty cursor is the final page, even when `partial` is true. A malformed row at
the probe position cannot make that page short while valid rows still remain:
the server keeps reading until the page is full or the backend is exhausted.

Legacy JSON-array responses keep their body shape:

- `GET /api/v1/events`
- `GET /api/v1/recordings/cameras/{id}/timeline`

The same flags are response headers:

- `X-VMS-Partial`: `true` or `false`
- `X-VMS-Skipped-Rows`: non-negative integer

An array shorter than the requested limit means the scan is exhausted. It does
not mean a malformed row truncated the page.

## Cursor

Event history walks newest first. The next request sends `before` and
`before_id` from the last returned valid event. The server reads past malformed
rows before deciding the page is final, so each valid event is returned once.

The recording index walks oldest first. The store applies the overlap predicate
before `LIMIT`: `segment_start < end` and the exclusive segment end is after
`start`. A non-overlapping lookback row cannot consume the page. `segments`
returns `next_after_segment_start` and `next_after_segment_id` when the valid
page is full and another row may exist. The next call passes those values as
`after_segment_start` and `after_segment_id`. Both are empty when the scan is
exhausted. The timeline and export routes fill one page up to
`recording_query_max_segments` and do not expose that cursor. That cap is a
separate bound from `X-VMS-Partial`. Callers that need a longer walk use the
client cursor. Point lookup is not this oldest-first page: `segment_for_start`
keeps rows that contain the instant, orders by latest start, and limits after
that filter.

A scan that cannot advance, or that exceeds the bounded batch cap, fails with
the existing store error and does not return a short page. The error text does
not include row contents.

## Observability

Each skipped row increments
`intelligent_vms_search_index_rows_skipped_total{source,reason}` and writes one
warning:

```text
event_search_row_skipped reason=invalid_row row_id=evt-1
recording_index_row_skipped reason=invalid_json row_id=-
```

`source` is `event_search` or `recording_index`. `reason` is `invalid_json`,
`invalid_shape`, or `invalid_row`. `row_id` is the event or segment id when it
matches a short safe token; otherwise it is `-`. Raw lines, attributes,
paths, URIs, and secrets are not logged.
