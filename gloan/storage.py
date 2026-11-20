"""Photo index (photos.json) and image-file handling: validation, cleaning, scanning, persistence."""

import json
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
    """Read the photo index; a missing file means an empty gallery."""
    try:
        return json.loads(INDEX_FILE.read_text())
    except FileNotFoundError:
        return []


def save_index(rows: list[Row]) -> None:
    """Write-then-rename so a crash mid-write cannot leave a truncated index."""
    tmp = INDEX_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(rows, indent=1))
    tmp.replace(INDEX_FILE)


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
            with path.open('rb') as fh:
                ext = sniff_ext(fh.read(16))
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
    """Size in bytes of a photo's file; 0 if the file is missing (callers normally filter those out first)."""
    try:
        return (PHOTO_DIR / f'{row["id"]}.{row["ext"]}').stat().st_size
    except OSError:
        return 0


def available_rows() -> list[Row]:
    """Index rows whose image file exists. Orphan rows are skipped silently (nothing shown, not counted) and
    left in photos.json untouched, so restoring the file, e.g. after a bucket hiccup, brings the photo back."""
    return [row for row in load_index() if (PHOTO_DIR / f'{row["id"]}.{row["ext"]}').is_file()]


def thumb_path(row: Row) -> Path | None:
    """Path of a photo's tile image, or None when it has none yet (or the stored name looks wrong).

    photos.json links tile to original through row['thumb'] (a file name inside THUMB_DIR). The name is
    re-validated against THUMB_FILE_RE so a hand-edited index can never point outside THUMB_DIR.
    """
    name = row.get('thumb')
    match = THUMB_FILE_RE.match(name) if isinstance(name, str) else None
    path = THUMB_DIR / name if match and match.group(1) == row['id'] else None
    return path if path and path.is_file() else None


def public_row(row: Row) -> Row:
    """Row as the API exposes it, with image URL, tile URL (None until generated) and file size (bytes)."""
    thumb = thumb_path(row)
    return {
        'story': '', **row,
        'url': f'/photos/{row["id"]}.{row["ext"]}',
        'thumbUrl': f'/thumbs/{thumb.name}' if thumb else None,
        'size': file_size(row),
    }
