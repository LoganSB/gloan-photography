"""Photo index (photos.json) and image-file handling: validation, cleaning, scanning, persistence and caching."""

import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from gloan.config import INDEX_FILE, MAX_STORY, PHOTO_DIR, THUMB_DIR, THUMB_FILE_RE


INDEX_LOCK = threading.Lock()  # guards photos.json read-modify-write across handler threads


Row = dict[str, Any]


def sniff_ext(data: bytes) -> str | None:
    """Trust the bytes, not the client's Content-Type, so only real images are stored.

    Args:
        data: Uploaded file contents.

    Return:
        Canonical extension, or None when the bytes are not a supported image.
    """
    result = None
    if data.startswith(b'\xff\xd8\xff'):
        result = 'jpg'
    elif data.startswith(b'\x89PNG\r\n\x1a\n'):
        result = 'png'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        result = 'webp'
    elif data[:6] in (b'GIF87a', b'GIF89a'):
        result = 'gif'
    return result


def clean_title(raw: object) -> str:
    """Collapse whitespace and cap the length of a user-supplied title."""
    return re.sub(r'\s+', ' ', str(raw)).strip()[:200]


def clean_story(raw: object) -> str:
    """Normalise line endings, limit blank-line runs and cap the length of a photo's story."""
    return re.sub(r'\n{3,}', '\n\n', str(raw).replace('\r\n', '\n').strip())[:MAX_STORY]


def clean_tags(tags: list[Any]) -> list[str]:
    """Normalise tags the same way the front end does: lowercase, hyphenated, unique, bounded."""
    out: list[str] = []
    for tag in tags:
        norm = re.sub(r'\s+', '-', str(tag).strip().lower())[:40]
        if norm and norm not in out:
            out.append(norm)
    return out[:30]


def load_index() -> list[Row]:
    """Read the photo index from disk; a missing file means an empty gallery."""
    try:
        return json.loads(INDEX_FILE.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []


_index_generation = 0  # bumped on every save or file removal so in-process caches notice changes immediately


def index_generation() -> int:
    """How many times this process has modified photos.json or photo files; caches compare it to know when to rebuild."""
    return _index_generation


def bump_index_generation() -> None:
    """Notify in-process caches (rows and tags) that files on disk were modified by this process."""
    global _index_generation  # pylint: disable=global-statement  # deliberate change counter for caches
    _index_generation += 1


def save_index(rows: list[Row]) -> None:
    """Write-then-rename so a crash mid-write cannot leave a truncated index."""
    temporary_file = INDEX_FILE.with_suffix('.tmp')
    temporary_file.write_text(json.dumps(rows, indent=1), encoding='utf-8')
    temporary_file.replace(INDEX_FILE)
    bump_index_generation()


class RowsCache:
    """In-memory snapshot of photos.json rows, photo file sizes and existing thumbnail filenames.

    Why cache this: every GET /api/photos needs the list of valid photos, their byte sizes (for size sorting
    and the public payload) and whether each photo has a tile in thumbs/. On a Cloud Storage FUSE mount each
    per-file stat() is a network round trip. Reading photos.json once and scanning photos/ and thumbs/ with
    os.scandir() reduces an entire rebuild to two directory reads, and subsequent requests do zero disk reads
    until photos.json, photos/ or thumbs/ change.
    """

    def __init__(self) -> None:
        photo_sizes: dict[str, int] = {}
        try:
            with os.scandir(PHOTO_DIR) as entries:
                for entry in entries:
                    try:
                        if entry.is_file():
                            photo_sizes[entry.name] = entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            pass

        thumb_names: set[str] = set()
        try:
            with os.scandir(THUMB_DIR) as entries:
                for entry in entries:
                    try:
                        if entry.is_file():
                            thumb_names.add(entry.name)
                    except OSError:
                        continue
        except OSError:
            pass

        self.photo_sizes = photo_sizes
        self.thumb_names = thumb_names
        # Orphan rows whose image file is missing are skipped silently and left in photos.json untouched.
        self.rows: list[Row] = [
            row for row in load_index()
            if f'{row["id"]}.{row["ext"]}' in photo_sizes
        ]


_cache_lock = threading.Lock()
_cached_rows: RowsCache | None = None
_cached_signature: tuple | None = None


def storage_signature() -> tuple:
    """Cheap fingerprint of photos.json, photos/ and thumbs/ on disk plus the in-process generation counter."""
    stats: list[tuple[int, int] | None] = []
    for path in (INDEX_FILE, PHOTO_DIR, THUMB_DIR):
        try:
            info = path.stat()
            stats.append((info.st_mtime_ns, info.st_size))
        except OSError:
            stats.append(None)
    return (_index_generation, *stats)


def get_rows_cache() -> RowsCache:
    """Return the current RowsCache snapshot, rebuilding only when storage_signature() changes.

    The signature is captured before reading disk so a concurrent modification during the build causes the
    very next call to rebuild rather than caching stale data under a newer signature.
    """
    global _cached_rows, _cached_signature  # pylint: disable=global-statement  # module-level snapshot cache
    signature = storage_signature()
    with _cache_lock:
        if _cached_rows is None or signature != _cached_signature:
            _cached_rows = RowsCache()
            _cached_signature = signature
        return _cached_rows


def scan_folder(skipped: list[str] | None = None) -> int:
    """Adopt image files copied into data/photos by hand: rename to a fresh id and add them to the index.

    Args:
        skipped: When given, filled with "name (reason)" for every entry that was not adopted and is not
            already indexed, so the caller can tell the owner why a copied file did not show up.

    Return:
        Number of photos adopted.
    """
    adopted = 0
    skipped = [] if skipped is None else skipped
    with INDEX_LOCK:
        rows = load_index()
        known = {f'{r["id"]}.{r["ext"]}' for r in rows}
        for path in sorted(PHOTO_DIR.iterdir()):
            if path.name in known or path.name.startswith('.'):
                continue
            if not path.is_file():
                skipped.append(f'{path.name} (folder: photos must sit directly in photos/)')
                continue
            with path.open('rb') as file_handle:
                ext = sniff_ext(file_handle.read(16))
            if not ext:
                skipped.append(f'{path.name} (not a jpeg/png/webp/gif)')
            else:
                pid = 'p' + secrets.token_hex(8)
                added = int(path.stat().st_mtime * 1000)  # keep the file's own date for ordering
                title = clean_title(re.sub(r'[_-]+', ' ', path.stem))
                path.rename(PHOTO_DIR / f'{pid}.{ext}')
                rows.append({'id': pid, 'ext': ext, 'title': title, 'tags': [], 'story': '', 'added': added})
                adopted += 1
        if adopted:
            save_index(rows)
    return adopted


def file_size(row: Row) -> int:
    """Size in bytes of a photo's file from the cache (or 0 if the file is missing)."""
    filename = f'{row["id"]}.{row["ext"]}'
    cache = get_rows_cache()
    if filename in cache.photo_sizes:
        return cache.photo_sizes[filename]
    try:
        return (PHOTO_DIR / filename).stat().st_size
    except OSError:
        return 0


def available_rows() -> list[Row]:
    """Index rows whose image file exists. Orphan rows are skipped silently (nothing shown, not counted) and
    left in photos.json untouched, so restoring the file, e.g. after a bucket hiccup, brings the photo back."""
    return [dict(row) for row in get_rows_cache().rows]


def thumb_path(row: Row) -> Path | None:
    """Path of a photo's tile image, or None when it has none yet (or the stored name looks wrong).

    photos.json links tile to original through row['thumb'] (a file name inside THUMB_DIR). The name is
    re-validated against THUMB_FILE_RE so a hand-edited index can never point outside THUMB_DIR.
    """
    name = row.get('thumb')
    match = THUMB_FILE_RE.match(name) if isinstance(name, str) else None
    if not match or match.group(1) != row['id']:
        return None
    cache = get_rows_cache()
    if name in cache.thumb_names or (THUMB_DIR / name).is_file():
        return THUMB_DIR / name
    return None


def public_row(row: Row) -> Row:
    """Row as the API exposes it, with image URL, tile URL (None until generated) and file size (bytes)."""
    thumb = thumb_path(row)
    return {
        'story': '', **row,
        'url': f'/photos/{row["id"]}.{row["ext"]}',
        'thumbUrl': f'/thumbs/{thumb.name}' if thumb else None,
        'size': file_size(row),
    }
