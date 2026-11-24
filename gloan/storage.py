"""Photo index (photos.json) and image-file handling: validation, cleaning, scanning, persistence and caching."""

import io
import json
import os
import re
import secrets
import struct
import threading
from pathlib import Path
from typing import Any

from gloan.config import INDEX_FILE, MAX_STORY, PHOTO_DIR, THUMB_DIR, THUMB_FILE_RE

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 60_000_000
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False


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


def _parse_dimensions_from_bytes(raw_bytes: bytes) -> tuple[int, int] | None:
    """Fast header-only (width, height) parser for PNG, GIF, JPEG and WebP when Pillow is unavailable."""
    if len(raw_bytes) >= 24 and raw_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        width, height = struct.unpack('>II', raw_bytes[16:24])
        return (width, height) if width > 0 and height > 0 else None
    if len(raw_bytes) >= 10 and raw_bytes[:6] in (b'GIF87a', b'GIF89a'):
        width, height = struct.unpack('<HH', raw_bytes[6:10])
        return (width, height) if width > 0 and height > 0 else None
    if len(raw_bytes) >= 30 and raw_bytes[:4] == b'RIFF' and raw_bytes[8:12] == b'WEBP':
        chunk = raw_bytes[12:16]
        if chunk == b'VP8X':
            width = 1 + int.from_bytes(raw_bytes[24:27], 'little')
            height = 1 + int.from_bytes(raw_bytes[27:30], 'little')
            return (width, height) if width > 0 and height > 0 else None
        if chunk == b'VP8 ' and raw_bytes[23:26] == b'\x9d\x01\x2a':
            width = struct.unpack('<H', raw_bytes[26:28])[0] & 0x3FFF
            height = struct.unpack('<H', raw_bytes[28:30])[0] & 0x3FFF
            return (width, height) if width > 0 and height > 0 else None
        if chunk == b'VP8L' and raw_bytes[20] == 0x2F:
            bits = int.from_bytes(raw_bytes[21:25], 'little')
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return (width, height) if width > 0 and height > 0 else None
    if raw_bytes.startswith(b'\xff\xd8'):
        offset = 2
        length = len(raw_bytes)
        while offset + 9 < length:
            if raw_bytes[offset] != 0xFF:
                break
            marker = raw_bytes[offset + 1]
            while marker == 0xFF and offset + 2 < length:
                offset += 1
                marker = raw_bytes[offset + 1]
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            if offset + 4 > length:
                break
            seg_len = struct.unpack('>H', raw_bytes[offset + 2:offset + 4])[0]
            if seg_len < 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if offset + 9 <= length:
                    height, width = struct.unpack('>HH', raw_bytes[offset + 5:offset + 9])
                    return (width, height) if width > 0 and height > 0 else None
                break
            offset += 2 + seg_len
    return None


def compute_aspect_ratio(source: Path | bytes) -> float | None:
    """Compute display `width / height` rounded to 4 decimals (honouring EXIF orientation when Pillow is installed)."""
    if _HAS_PILLOW:
        try:
            stream: Any = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
            with Image.open(stream) as img:
                width, height = img.size
                # Swap dimensions if EXIF Orientation tag (274) rotates 90/270 degrees (values 5, 6, 7, 8).
                exif = img.getexif()
                if exif and exif.get(274) in (5, 6, 7, 8):
                    width, height = height, width
                if width > 0 and height > 0:
                    return round(width / height, 4)
        except Exception:  # noqa: BLE001
            pass
    try:
        if isinstance(source, (bytes, bytearray)):
            header = bytes(source[:65536])
        else:
            with source.open('rb') as handle:
                header = handle.read(65536)
        dims = _parse_dimensions_from_bytes(header)
        if dims and dims[1] > 0:
            return round(dims[0] / dims[1], 4)
    except OSError:
        pass
    return None


def clean_title(raw: object) -> str:
    """Strip control characters, collapse whitespace and cap the length of a user-supplied title."""
    without_controls = re.sub(r'[\x00-\x1f\x7f]', ' ', str(raw))
    return re.sub(r'\s+', ' ', without_controls).strip()[:200]


def clean_story(raw: object) -> str:
    """Strip non-newline control characters, normalise line endings, limit blank-line runs and cap story length."""
    normalised = str(raw).replace('\r\n', '\n').replace('\r', '\n')
    without_controls = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', normalised)
    return re.sub(r'\n{3,}', '\n\n', without_controls.strip())[:MAX_STORY]


def clean_tags(tags: list[Any]) -> list[str]:
    """Normalise tags the same way the front end does: lowercase, hyphenated, unique, bounded."""
    if not isinstance(tags, (list, tuple)):
        return []
    out: list[str] = []
    for tag in tags:
        without_controls = re.sub(r'[\x00-\x1f\x7f,]', ' ', str(tag))
        norm = re.sub(r'\s+', '-', without_controls.strip().lower())[:40]
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
    updated_meta = False
    skipped = [] if skipped is None else skipped
    with INDEX_LOCK:
        rows = load_index()
        known = {f'{r["id"]}.{r["ext"]}' for r in rows}
        # Backfill missing aspectRatio metadata in photos.json for existing photos (reusing thumbnails when present).
        for existing_row in rows:
            if not existing_row.get('aspectRatio'):
                thumb_file = THUMB_DIR / f'{existing_row["id"]}.jpg'
                photo_file = PHOTO_DIR / f'{existing_row["id"]}.{existing_row["ext"]}'
                source_file = thumb_file if thumb_file.is_file() else (photo_file if photo_file.is_file() else None)
                if source_file is not None:
                    ratio = compute_aspect_ratio(source_file)
                    if ratio:
                        existing_row['aspectRatio'] = ratio
                        updated_meta = True
        for path in sorted(PHOTO_DIR.iterdir()):
            if path.name in known or path.name.startswith('.'):
                continue
            if path.is_symlink() or not path.is_file():
                skipped.append(f'{path.name} (not a regular file)')
                continue
            with path.open('rb') as file_handle:
                ext = sniff_ext(file_handle.read(16))
            if not ext:
                skipped.append(f'{path.name} (not a jpeg/png/webp/gif)')
            else:
                pid = 'p' + secrets.token_hex(8)
                added = int(path.stat().st_mtime * 1000)  # keep the file's own date for ordering
                title = clean_title(re.sub(r'[_-]+', ' ', path.stem))
                dest = PHOTO_DIR / f'{pid}.{ext}'
                path.rename(dest)
                ratio = compute_aspect_ratio(dest)
                new_row: Row = {'id': pid, 'ext': ext, 'title': title, 'tags': [], 'story': '', 'added': added}
                if ratio:
                    new_row['aspectRatio'] = ratio
                rows.append(new_row)
                adopted += 1
        if adopted or updated_meta:
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

    Checks `row['thumb']` first and falls back to the canonical `<id>.jpg` filename in `THUMB_DIR` so tiles
    present on the Cloud Storage bucket are served immediately even before `photos.json` is rewritten.
    The name is validated against THUMB_FILE_RE so a hand-edited index can never point outside THUMB_DIR.
    """
    name = row.get('thumb') or f'{row["id"]}.jpg'
    match = THUMB_FILE_RE.match(name) if isinstance(name, str) else None
    if not match or match.group(1) != row['id']:
        return None
    cache = get_rows_cache()
    if name in cache.thumb_names:
        return THUMB_DIR / name
    return None


def all_row_tags(row: Row) -> list[str]:
    """Return the combined manual/uploaded `tags` and AI-generated `aiTags` for a photo (manual tags first)."""
    combined: list[str] = []
    for tag in [*(row.get('tags') or []), *(row.get('aiTags') or [])]:
        if isinstance(tag, str) and tag and tag not in combined:
            combined.append(tag)
    return combined


def public_row(row: Row) -> Row:
    """Row as the API exposes it, keeping manual `tags` and AI `aiTags` tracked separately with `aspectRatio`."""
    thumb = thumb_path(row)
    manual_tags = clean_tags(row.get('tags') or [])
    ai_tags = clean_tags(row.get('aiTags') or [])
    ratio = row.get('aspectRatio')
    if not isinstance(ratio, (int, float)) or ratio <= 0:
        ratio = compute_aspect_ratio(thumb) if thumb is not None else compute_aspect_ratio(PHOTO_DIR / f'{row["id"]}.{row["ext"]}')
    return {
        'story': '',
        **row,
        'tags': manual_tags,
        'aiTags': ai_tags,
        'aspectRatio': round(float(ratio), 4) if ratio else 1.3333,
        'url': f'/photos/{row["id"]}.{row["ext"]}',
        'thumbUrl': f'/thumbs/{thumb.name}' if thumb else None,
        'size': file_size(row),
    }
