"""Filtering, sorting and paging of the photo list for GET /api/photos."""

from typing import Any

from gloan.config import DEFAULT_PAGE_SIZE, MAX_FILTER_TAGS, MAX_PAGE_SIZE
from gloan.storage import available_rows, file_size, public_row
from gloan.tags import get_tag_index


# sort name -> (key function, newest/largest/Z-first?). Names must match SORTS in index.html.
# Size sorting stats every file, so it costs more than the others; fine for a personal gallery.
SORT_KEYS = {
    'new': (lambda row: row['added'], True),
    'old': (lambda row: row['added'], False),
    'az': (lambda row: row.get('title', '').lower(), False),
    'za': (lambda row: row.get('title', '').lower(), True),
    'big': (file_size, True),
    'small': (file_size, False),
}


def parse_int(raw: str | None, default: int, minimum: int, maximum: int) -> int:
    """Query-string integer, clamped into [minimum, maximum]; junk or missing falls back to default."""
    try:
        return max(minimum, min(maximum, int(raw or '')))
    except ValueError:
        return default


def query_photos(params: dict[str, list[str]]) -> dict[str, Any]:
    """One filtered, sorted page of photos plus the gallery-wide numbers the page needs.

    Args:
        params: Parsed query string. page (0-based), size, sort (SORT_KEYS name), tags (comma-separated,
            matched exactly as stored) and match ('any' or 'all').

    Return:
        photos (this page), total (matching all pages), all (whole gallery), page (clamped), pages, size,
        tags ([[tag, count]] over the whole gallery, so filter chips never depend on the current page).
    """
    rows = available_rows()
    first_value = lambda name: (params.get(name) or [None])[0]  # noqa: E731
    size = parse_int(first_value('size'), DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    wanted = [tag for tag in (first_value('tags') or '').split(',') if tag][:MAX_FILTER_TAGS]
    require_all = first_value('match') == 'all'
    sort_key, descending = SORT_KEYS.get(first_value('sort') or '', SORT_KEYS['new'])

    if wanted:
        combine = all if require_all else any
        matching = [row for row in rows if combine(tag in row['tags'] for tag in wanted)]
    else:
        matching = rows
    matching = sorted(matching, key=sort_key, reverse=descending)

    pages = max(1, -(-len(matching) // size))  # ceiling division
    page = parse_int(first_value('page'), 0, 0, pages - 1)  # a delete may leave the client past the last page
    return {
        'photos': [public_row(row) for row in matching[page * size:(page + 1) * size]],
        'total': len(matching),
        'all': len(rows),
        'page': page,
        'pages': pages,
        'size': size,
        'tags': get_tag_index().ordered,  # cached; rebuilt only when photos.json or photos/ change
    }
