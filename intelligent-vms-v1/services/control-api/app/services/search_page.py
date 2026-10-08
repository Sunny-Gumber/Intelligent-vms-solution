"""Shared page contract for event search and the recording index.

A page stays a list of valid rows so existing callers keep working. Malformed
backend rows are counted and skipped. The scan keeps reading until the page is
full or the backend is exhausted, so a skip cannot look like the final page.
"""

import logging
import re

from prometheus_client import Counter


log = logging.getLogger(__name__)

_SAFE_ROW_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SKIP_REASONS = frozenset({"invalid_json", "invalid_shape", "invalid_row"})
_SOURCES = frozenset({"event_search", "recording_index"})
_MAX_SCAN_BATCHES = 10000

ROWS_SKIPPED = Counter(
    "intelligent_vms_search_index_rows_skipped_total",
    "Malformed rows skipped while filling an event-search or recording-index page.",
    ["source", "reason"],
)


class _BatchScan:
    def __init__(self, skipped, last_cursor, trailing_unkeyed, consumed_all):
        self.skipped = skipped
        self.last_cursor = last_cursor
        self.trailing_unkeyed = trailing_unkeyed
        self.consumed_all = consumed_all


class RowPage(list):
    """Valid rows from one scan, with explicit malformed-row accounting.

    The value compares and iterates as a list. `partial` is true when this scan
    skipped at least one malformed row. `skipped_rows` is that count.
    `exhausted` is true when the backend has no further row past this page.
    """

    def __init__(self, rows, *, skipped_rows: int, exhausted: bool):
        super().__init__(rows)
        self.skipped_rows = skipped_rows
        self.partial = skipped_rows > 0
        self.exhausted = exhausted


class EventSearchPage(RowPage):
    """One event-search page of valid event dictionaries.

    Existing callers can keep treating the value as a list. `partial` and
    `skipped_rows` describe malformed ClickHouse lines skipped while filling
    the requested limit. `exhausted` is true when a further valid event does
    not exist beyond this page.
    """


class RecordingIndexPage(RowPage):
    """One recording-index page of valid segment dictionaries.

    `next_after_segment_start` and `next_after_segment_id` are set together
    when this page is full and the backend may hold another valid segment.
    Both are empty when the scan is exhausted, including a short final page
    whose only missing rows were malformed.
    """

    def __init__(
        self,
        rows,
        *,
        skipped_rows: int,
        exhausted: bool,
        next_after_segment_start=None,
        next_after_segment_id=None,
    ):
        super().__init__(rows, skipped_rows=skipped_rows, exhausted=exhausted)
        if exhausted or not rows:
            next_after_segment_start = None
            next_after_segment_id = None
        self.next_after_segment_start = next_after_segment_start
        self.next_after_segment_id = next_after_segment_id


def apply_page_headers(response, page) -> None:
    """Repeat partial-page accounting on a legacy JSON-array response.

    Args:
        response: HTTP response whose headers can be set before it is returned.
        page: Page value exposing `partial` and `skipped_rows`, or None when
            this response did not scan a backend page.

    Returns:
        None. Array bodies are left unchanged.

    Raises:
        No domain exception is raised for a missing or non-numeric skip count;
        those inputs are reported as zero skips.
    """
    partial = bool(getattr(page, "partial", False))
    try:
        skipped_rows = int(getattr(page, "skipped_rows", 0))
    except (TypeError, ValueError):
        skipped_rows = 0
    if skipped_rows < 0:
        skipped_rows = 0
    response.headers["X-VMS-Partial"] = "true" if partial else "false"
    response.headers["X-VMS-Skipped-Rows"] = str(skipped_rows)


def _safe_row_id(value):
    if isinstance(value, str) and _SAFE_ROW_ID.fullmatch(value):
        return value
    return None


def _record_skipped(source: str, reason: str, row_id) -> None:
    bounded_source = source if source in _SOURCES else "event_search"
    bounded_reason = reason if reason in _SKIP_REASONS else "invalid_row"
    ROWS_SKIPPED.labels(source=bounded_source, reason=bounded_reason).inc()
    log.warning(
        "%s_row_skipped reason=%s row_id=%s",
        bounded_source,
        bounded_reason,
        _safe_row_id(row_id) or "-",
    )


def _consume_batch(lines, collected, limit, interpret, source) -> _BatchScan:
    skipped = 0
    last_cursor = None
    trailing_unkeyed = 0
    consumed_all = True
    for index, line in enumerate(lines):
        row, cursor, reason = interpret(line)
        if reason is not None:
            skipped += 1
            _record_skipped(source, reason, None if cursor is None else cursor[1])
            if cursor is None:
                trailing_unkeyed += 1
            else:
                last_cursor = cursor
                trailing_unkeyed = 0
        else:
            if row is not None:
                collected.append(row)
            if cursor is None:
                trailing_unkeyed += 1
            else:
                last_cursor = cursor
                trailing_unkeyed = 0
        if len(collected) >= limit:
            consumed_all = index == len(lines) - 1
            break
    return _BatchScan(skipped, last_cursor, trailing_unkeyed, consumed_all)


def _progress_token(cursor, offset) -> tuple:
    if cursor is None:
        return ("", "", offset)
    timestamp, row_id = cursor
    return (timestamp.isoformat(), row_id, offset)
