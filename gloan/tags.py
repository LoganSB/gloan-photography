"""Cached tag index and typo-tolerant tag search (autocomplete) behind GET /api/tags.

Why a cache: the search box asks for suggestions on (almost) every keystroke, and rebuilding the tag list means
reading photos.json and checking that every photo file exists. The index below is rebuilt only when its sources
change, so a normal lookup costs two stat() calls plus a scan over the short list of distinct tags, and repeated
queries are answered straight from a per-index result cache.
"""

import functools
import re
import threading
import unicodedata
from typing import Any

from gloan.config import INDEX_FILE, MAX_TAG_QUERY, PHOTO_DIR
from gloan.storage import Row, all_row_tags, available_rows, index_generation

FUZZY_MIN_LENGTH = 3  # shorter queries: one typo would match almost everything, so only exact/prefix/contains apply
MAX_WILDCARD_STARS = 2  # more '*' could make the regex backtrack heavily; extra ones are dropped
RESULT_CACHE_SIZE = 256  # remembered queries per index; the whole cache goes away when the index is rebuilt

# Lower rank = better match. The names double as the 'match' label sent to the browser.
MATCH_RANKS = {'exact': 0, 'prefix': 1, 'word': 2, 'contains': 3, 'wildcard': 3, 'fuzzy': 4}


def search_key(text: str) -> str:
    """Normalise text for matching: lowercase, accents removed, runs of spaces/underscores/hyphens -> one '-'.

    So 'Single Subject', 'single_subject' and 'single-subject' compare equal, and 'cafe' finds 'café'.
    """
    decomposed = unicodedata.normalize('NFKD', str(text))
    without_accents = ''.join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r'[\s_-]+', '-', without_accents.strip().lower()).strip('-')


@functools.lru_cache(maxsize=RESULT_CACHE_SIZE)
def fuzzy_regex(key: str) -> re.Pattern[str]:
    """Regex matching text within ONE typo of `key`, at the start of a tag or of any word inside it.

    The classic one-edit alternation: every way of making a single mistake becomes its own branch, with '.'
    standing for "any one character":
      wrong letter      mo.ntain     (substitution)
      left a letter out mounta.in    (the tag has one more character than the query)
      one letter extra  mounain      (that query character is dropped)
      swapped letters   moutnain     (two neighbours transposed)
    Every typed character goes through re.escape(), so user input can never inject regex syntax, and there
    are no nested quantifiers, so matching cannot backtrack catastrophically. There is no end anchor, so it
    also works as an autocomplete prefix while the user is still typing.
    """
    tokens = [re.escape(char) for char in key]
    variants: set[tuple[str, ...]] = set()
    for position in range(len(tokens)):
        before, after = tokens[:position], tokens[position + 1:]
        variants.add((*before, '.', *after))  # wrong letter
        variants.add((*before, *after))  # one letter too many in the query
        if after:  # neighbours swapped
            variants.add((*before, after[0], tokens[position], *after[1:]))
    for position in range(len(tokens) + 1):  # query is missing one letter
        variants.add((*tokens[:position], '.', *tokens[position:]))
    # Very short variants (e.g. 'ca' from 'cat') would match half the gallery, so they are left out.
    spellings = sorted((''.join(variant) for variant in variants if len(variant) >= FUZZY_MIN_LENGTH),
                       key=len, reverse=True)  # longest first: most specific spellings are tried first
    return re.compile(r'(?:^|-)(?:' + '|'.join(spellings) + ')')


@functools.lru_cache(maxsize=RESULT_CACHE_SIZE)
def wildcard_regex(key: str) -> re.Pattern[str]:
    """Shell-style pattern: '*' = any run of characters, '?' = exactly one, everything else literal.

    Runs of '*' collapse to one and only the first MAX_WILDCARD_STARS act as wildcards, because each extra
    '.*' multiplies the work the regex engine may need on a non-matching tag.
    """
    parts: list[str] = []
    stars_used = 0
    for char in re.sub(r'\*+', '*', key):
        if char == '*':
            if stars_used < MAX_WILDCARD_STARS:
                parts.append('.*')
                stars_used += 1
        elif char == '?':
            parts.append('.')
        else:
            parts.append(re.escape(char))
    return re.compile(''.join(parts))


def classify_match(entry_key: str, key: str) -> str | None:
    """Best way `entry_key` (a normalised tag) matches the normalised query `key`, or None for no match.

    When `key` contains '*' or '?', wildcards match anywhere from `key` unless the caller explicitly anchored
    the end with '?'; trailing characters after a prefix pattern (e.g. `*wint` matching `bryce-winter`) are
    allowed automatically so typing a partial wildcard still autocompletes cleanly.
    """
    if '*' in key or '?' in key:
        pattern = key if key.endswith('*') else key + '*'
        return 'wildcard' if wildcard_regex(pattern).fullmatch(entry_key) else None
    result = None
    if entry_key == key:
        result = 'exact'
    elif entry_key.startswith(key):
        result = 'prefix'
    elif re.search(r'(?:^|-)' + re.escape(key), entry_key):
        result = 'word'  # 'subj' finds 'single-subject'
    elif key in entry_key:
        result = 'contains'
    elif len(key) >= FUZZY_MIN_LENGTH and fuzzy_regex(key).search(entry_key):
        result = 'fuzzy'
    return result


def parse_query_terms(query: str) -> list[str]:
    """Split a comma-separated search query into normalised non-empty search keys.

    Allows multi-term intersection queries like `*winter, bryce`, where every comma-separated term must match
    the candidate tag.
    """
    raw_parts = query[:MAX_TAG_QUERY * 4].split(',')
    terms: list[str] = []
    for part in raw_parts[:8]:
        term = search_key(part[:MAX_TAG_QUERY])
        if term and term not in terms:
            terms.append(term)
    return terms


def classify_multi_match(entry_key: str, terms: list[str]) -> str | None:
    """Match `entry_key` against ALL comma-separated `terms`; return the weakest match label or None if any fails."""
    if not terms:
        return None
    worst_rank = -1
    worst_label: str | None = None
    for term in terms:
        match = classify_match(entry_key, term)
        if not match:
            return None
        rank = MATCH_RANKS[match]
        if rank > worst_rank:
            worst_rank = rank
            worst_label = match
    return worst_label


class TagIndex:
    """Snapshot of every tag in use: counts, popularity order and pre-computed search keys.

    Treated as read-only once built; a change to the data produces a new TagIndex rather than an update.
    """

    def __init__(self, rows: list[Row]) -> None:
        counts: dict[str, int] = {}
        for row in rows:
            for tag in all_row_tags(row):
                counts[tag] = counts.get(tag, 0) + 1
        self.counts = counts
        # Most-used first, then alphabetical: the order of the filter chips and of empty-query suggestions.
        self.ordered: list[tuple[str, int]] = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        # (search key, tag, count), normalised once here instead of on every keystroke.
        self.entries = [(search_key(tag), tag, count) for tag, count in self.ordered]
        self._results: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._results_lock = threading.Lock()

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Up to `limit` suggestions for the single active comma-delimited token `query`, best match first."""
        key = search_key(query[:MAX_TAG_QUERY])
        cache_key = (key, limit)
        with self._results_lock:
            cached = self._results.get(cache_key)
        if cached is None:
            cached = self._rank(key, limit)
            with self._results_lock:
                if len(self._results) >= RESULT_CACHE_SIZE:
                    self._results.clear()  # crude bound; recomputing a few queries is cheap
                self._results[cache_key] = cached
        return cached

    def _rank(self, key: str, limit: int) -> list[dict[str, Any]]:
        """Match every tag against `key` and order by match quality, then alphabetical tag name."""
        if not key:
            return [{'tag': tag, 'count': count, 'match': 'popular'} for _, tag, count in self.entries[:limit]]
        matches = []
        for entry_key, tag, count in self.entries:
            match = classify_match(entry_key, key)
            if match:
                matches.append({'tag': tag, 'count': count, 'match': match})
        # Order non-empty search matches by match rank first, then alphabetically by tag name so Tab autofill
        # predictably completes the alphabetically-first matching tag (e.g. "bry" -> "bryce_fall,").
        matches.sort(key=lambda item: (MATCH_RANKS[item['match']], item['tag'].lower()))
        return matches[:limit]

    def matching_tags_for_term(self, term_query: str) -> set[str]:
        """Set of all tag names in the gallery that match a single search term (exact/prefix/word/wildcard/fuzzy)."""
        key = search_key(term_query[:MAX_TAG_QUERY])
        if not key:
            return set()
        return {tag for entry_key, tag, _ in self.entries if classify_match(entry_key, key) is not None}


def row_matches_tag_term(row: Row, term_query: str) -> bool:
    """True when `row` has at least one manual or AI tag matching `term_query` (exact, prefix, wildcard or fuzzy)."""
    key = search_key(term_query[:MAX_TAG_QUERY])
    if not key:
        return True
    for tag in all_row_tags(row):
        if classify_match(search_key(tag), key) is not None:
            return True
    return False


def row_matches_all_tag_terms(row: Row, raw_terms: list[str]) -> bool:
    """True when `row` satisfies EVERY comma-separated tag filter/search term (AND matching).

    Wait: what if a single photo has ONE compound tag like `bryce_winter` and the user searches `*winter, bryce`,
    OR a photo has two separate tags `['bryce', 'winter']` and the user searches `*winter, bryce`?
    Because `row_matches_tag_term(row, term)` checks whether the photo has ANY tag matching `term`, and we require
    `all(row_matches_tag_term(row, term) for term in terms)`:
    - A photo with `['bryce_winter']` matches `*winter` (via `bryce_winter`) AND matches `bryce` (via `bryce_winter`)!
    - A photo with `['bryce', 'winter']` matches `*winter` (via `winter`) AND matches `bryce` (via `bryce`)!
    - A photo with `['arches_winter']` matches `*winter`, but fails `bryce`!
    - A photo with `['bryce_summer']` matches `bryce`, but fails `*winter`!
    So both compound tags (`bryce_winter`) and separate multi-tags (`bryce` + `winter`) work seamlessly under AND!
    """
    return all(row_matches_tag_term(row, term) for term in raw_terms if search_key(term))


_cache_lock = threading.Lock()  # guards the two globals below across handler threads
_cached_index: TagIndex | None = None
_cached_signature: tuple | None = None


def _source_signature() -> tuple:
    """Cheap fingerprint of what the index is built from. It changes when:

    - this process saves photos.json (index_generation bumps, even within one filesystem timestamp tick),
    - another process rewrites photos.json, e.g. tools/make_thumbnails.py (its mtime/size change),
    - photo files are added to or removed from photos/ by hand (the folder's mtime changes).
    """
    stats: list[tuple[int, int] | None] = []
    for path in (INDEX_FILE, PHOTO_DIR):
        try:
            info = path.stat()
            stats.append((info.st_mtime_ns, info.st_size))
        except OSError:
            stats.append(None)
    return (index_generation(), *stats)


def get_tag_index() -> TagIndex:
    """The current TagIndex, rebuilt only when the source signature has changed since the last build.

    The signature is taken before the rows are read: if data changes in between, the stored signature is the
    older one, so the next call simply rebuilds again. The cache can lag one call, never stay stale.
    """
    global _cached_index, _cached_signature  # pylint: disable=global-statement  # deliberate module cache
    signature = _source_signature()
    with _cache_lock:
        if _cached_index is None or signature != _cached_signature:
            _cached_index = TagIndex(available_rows())
            _cached_signature = signature
        return _cached_index


def suggest_tags(query: str, limit: int) -> dict[str, Any]:
    """Reply body for GET /api/tags: suggestions for the active (last) comma-separated segment of `query`.

    When `query` has multiple comma-separated terms (e.g. `*winter, bryce`) and the caller also wants to know
    which tags match ALL terms simultaneously, `matchingAll` lists the tags matching every term while `suggestions`
    autocompletes the active segment after the last comma.
    """
    segments = query[:MAX_TAG_QUERY * 4].split(',')
    active_segment = segments[-1]
    index = get_tag_index()
    suggestions = index.search(active_segment, limit)
    terms = parse_query_terms(query)
    if len(terms) > 1:
        matching_all = [
            {'tag': tag, 'count': count, 'match': classify_multi_match(entry_key, terms)}
            for entry_key, tag, count in index.entries
            if classify_multi_match(entry_key, terms) is not None
        ]
        matching_all.sort(key=lambda item: (MATCH_RANKS[item['match']], item['tag'].lower()))
    else:
        matching_all = suggestions
    return {
        'query': search_key(active_segment[:MAX_TAG_QUERY]),
        'suggestions': suggestions,
        'matchingAll': matching_all[:limit],
    }
