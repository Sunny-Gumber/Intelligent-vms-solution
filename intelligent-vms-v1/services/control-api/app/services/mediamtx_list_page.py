"""MediaMTX v1.21.1 list-page contract shared by both enumeration walks.

Control-api imports this module. The node agent loads this same file from the
repository tree or from the copy placed beside ``main.py`` in the node image.
Counts are never invented from the length of ``items``.
"""

MEDIAMTX_LIST_PAGE_SIZE = 100


class MediaMTXListPageError(ValueError):
    """Raised when a list page is not an object with an items array.

    A bad count is not this error. The page is then an untrusted catalog and
    the walk fails closed without rewriting ``itemCount`` or ``pageCount``.
    """


def _exact_count(value: object) -> int | None:
    """Return a non-negative integer count, rejecting bool, float, and str.

    Args:
        value: Raw ``itemCount`` or ``pageCount`` field, or None when absent.

    Returns:
        The count when it is an exact non-negative ``int``. None for every
        other type, including ``bool`` and negative numbers.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _expected_page_count(item_count: int, per_page: int) -> int:
    """Return the v1.21.1 page count for a total at one page size.

    Args:
        item_count: Upstream ``itemCount``.
        per_page: Page size requested from MediaMTX. The pinned default is 100.

    Returns:
        Zero when the catalog is empty, otherwise the ceiling division.
    """
    if item_count == 0:
        return 0
    return (item_count + per_page - 1) // per_page


def _item_names(items: list) -> list[str]:
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise MediaMTXListPageError("MediaMTX list item was not an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise MediaMTXListPageError("MediaMTX list item was missing a name")
        names.append(name)
    return names


def _page_is_consistent(
    *,
    item_count: int | None,
    page_count: int | None,
    page: int,
    size: int,
    per_page: int,
) -> bool:
    if item_count is None or page_count is None:
        return False
    if page_count != _expected_page_count(item_count, per_page):
        return False
    if item_count == 0:
        return page == 0 and size == 0 and page_count == 0
    if page < 0 or page >= page_count:
        return False
    remaining = item_count - page * per_page
    if remaining <= 0:
        return False
    return size == min(per_page, remaining)


def assess_mediamtx_list_page(
    body: object,
    *,
    page: int,
    per_page: int = MEDIAMTX_LIST_PAGE_SIZE,
) -> dict:
    """Judge one MediaMTX list page without inventing totals.

    A page is accepted only when ``itemCount`` and ``pageCount`` are both
    present, both exact non-negative integers, and both agree with the v1.21.1
    page size and with the number of items on this page. An empty catalog is
    accepted only as ``itemCount`` 0, ``pageCount`` 0, and ``items`` [].

    Args:
        body: Decoded JSON page.
        page: Zero-based page index that was requested.
        per_page: Page size that was requested. The pinned default is 100.

    Returns:
        ``accepted``, the upstream counts (or None when a count is missing or
        not an exact non-negative integer), ``items``, and ``names``.
        ``accepted`` false means the catalog is untrusted. The counts are the
        upstream values that parsed, never ``len(items)`` and never a
        synthesized page count of 1.

    Raises:
        MediaMTXListPageError: If the body is not an object, ``items`` is not
            a list, or an entry is not an object with a non-empty name.
    """
    if not isinstance(body, dict):
        raise MediaMTXListPageError("MediaMTX list response was not an object")
    if "items" not in body or not isinstance(body["items"], list):
        raise MediaMTXListPageError("MediaMTX list items was not a list")
    items = body["items"]
    names = _item_names(items)
    item_count = _exact_count(body.get("itemCount"))
    page_count = _exact_count(body.get("pageCount"))
    return {
        "accepted": _page_is_consistent(
            item_count=item_count,
            page_count=page_count,
            page=page,
            size=len(items),
            per_page=per_page,
        ),
        "item_count": item_count,
        "page_count": page_count,
        "items": items,
        "names": names,
    }
