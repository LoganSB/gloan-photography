"""Low-cost / zero-cost AI tag bot for admin uploads and manual folder rescans.

Design goals:
1. Near-zero or zero cloud cost:
   - When `GLOAN_GEMINI_API_KEY` (or `GEMINI_API_KEY`) is set, calls Google AI Studio's free-tier lightweight vision
     model (`gemini-2.5-flash-lite` by default, configurable via `GLOAN_GEMINI_MODEL`).
   - Reuses existing 480px JPEG thumbnails from `data/thumbs/` (via `thumb_path(row)`) or downscales in memory with
     Pillow to 384px JPEG (~15-30 KB) before calling the API, avoiding reading or uploading 30-50 MB originals over
     Cloud Storage FUSE.
   - Caps output tokens (`maxOutputTokens: 80`) and per-rescan batch size (`MAX_AI_IMAGES_PER_RUN = 25`) so a rescan
     can never incur unexpected cloud CPU or API charges.
2. Always guarantees at least `MIN_AI_TAGS = 5` tags at $0.00 fallback cost:
   - Combines Gemini's tags (when configured) with a fast, in-process visual & metadata analyzer (aspect ratio,
     resolution, luminance/contrast, natural color palette regions using Pillow, and title tokens) so every image
     without AI tags receives at least 5 tags even with no API key or when offline.
3. Tracked in `photos.json`:
   - Records `row['aiTags']` (list of >= 5 AI-generated tags) on each tagged row and merges them into `row['tags']`.
   - Any image with a non-empty `row['aiTags']` list is skipped on future rescans.
"""

import base64
import json
import os
import re
import struct
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from gloan.config import LOGGER, MIME, PHOTO_DIR
from gloan.storage import (
    INDEX_LOCK,
    Row,
    available_rows,
    clean_tags,
    load_index,
    save_index,
    sniff_ext,
    thumb_path,
)

try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 60_000_000
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

MIN_AI_TAGS = 5
MAX_AI_TAGS = 8
MAX_AI_IMAGES_PER_RUN = 25
GEMINI_DEFAULT_MODEL = 'gemini-2.5-flash-lite'
GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'

# Fallback general photography tags used only when an image is tiny/synthetic and still needs to reach MIN_AI_TAGS (5).
BASELINE_PHOTO_TAGS = (
    'photography',
    'digital-image',
    'visual-study',
    'composition',
    'still-frame',
    'gallery',
)

STOP_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'into', 'over', 'under', 'near', 'photo', 'image',
    'img', 'dsc', 'pxl', 'lmp', 'raw', 'untitled', 'copy', 'edit', 'final', 'new', 'old',
}


def has_ai_tags(row: Row) -> bool:
    """True when `row` in photos.json already has AI-generated tags recorded."""
    ai_tags = row.get('aiTags')
    return isinstance(ai_tags, list) and len(ai_tags) >= MIN_AI_TAGS


def gemini_api_key() -> str:
    """Return the configured Gemini API key (free-tier or paid), or '' when not configured."""
    return (os.environ.get('GLOAN_GEMINI_API_KEY') or os.environ.get('GEMINI_API_KEY') or '').strip()


def gemini_model_name() -> str:
    """Return the Gemini model name to use; defaults to free-tier friendly `gemini-2.5-flash-lite`."""
    model = os.environ.get('GLOAN_GEMINI_MODEL', '').strip() or GEMINI_DEFAULT_MODEL
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,80}', model):
        return GEMINI_DEFAULT_MODEL
    return model


def _prepare_compact_image_bytes(row: Row, raw_bytes: bytes | None = None) -> tuple[bytes, str]:
    """Return a compact `(image_bytes, mime_type)` tuple for vision analysis.

    Reuses the existing 480px JPEG thumbnail in `data/thumbs/` first so we never read a 50 MB original over FUSE
    when a 50 KB tile already exists. Otherwise downscales in memory to <= 384px JPEG when Pillow is installed.
    """
    existing_thumb = thumb_path(row)
    if existing_thumb is not None:
        try:
            return existing_thumb.read_bytes(), 'image/jpeg'
        except OSError:
            pass

    source_path = PHOTO_DIR / f'{row["id"]}.{row["ext"]}'
    if raw_bytes is None:
        try:
            # Cap raw read at 25 MB if Pillow is unavailable; Pillow opens via file path streaming.
            if not HAS_PILLOW:
                with source_path.open('rb') as handle:
                    raw_bytes = handle.read(65536)
        except OSError:
            return b'', 'image/jpeg'

    if HAS_PILLOW:
        try:
            import io
            stream_source: Any = io.BytesIO(raw_bytes) if raw_bytes is not None else source_path
            with Image.open(stream_source) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((384, 384), Image.Resampling.BILINEAR)
                buffer = io.BytesIO()
                image.convert('RGB').save(buffer, 'JPEG', quality=72, optimize=True)
                return buffer.getvalue(), 'image/jpeg'
        except Exception:  # noqa: BLE001
            pass

    if raw_bytes is None:
        try:
            with source_path.open('rb') as handle:
                raw_bytes = handle.read(65536)
        except OSError:
            raw_bytes = b''
    ext = sniff_ext(raw_bytes) or str(row.get('ext', 'jpg'))
    return raw_bytes, MIME.get(ext, 'image/jpeg')


def _query_gemini_free_tier_tags(image_bytes: bytes, mime_type: str, title: str = '') -> list[str]:
    """Call Google Gemini Flash-Lite (`generateContent`) for 5-8 concise tags when an API key is configured.

    Returns an empty list if no key is configured or if the network/quota call fails, allowing seamless fallback
    to the zero-cost local visual analyzer.
    """
    api_key = gemini_api_key()
    if not api_key or not image_bytes or len(image_bytes) > 4 * 1024 * 1024:
        return []

    model = gemini_model_name()
    url = f'{GEMINI_API_BASE}/{model}:generateContent?key={api_key}'
    title_hint = f' Title hint: "{title[:80]}".' if title else ''
    prompt = (
        'You are tagging a photography portfolio. Return 6 to 8 concise lowercase photography tags '
        '(single words or hyphenated compounds like mountain, sunset, forest, macro, water, winter, '
        'long-exposure, wildlife, desert, astrophotography) describing the subject, scenery, lighting, '
        f'season, and mood.{title_hint} Reply with ONLY a comma-separated list of tags.'
    )
    payload = {
        'contents': [{
            'parts': [
                {'text': prompt},
                {
                    'inline_data': {
                        'mime_type': mime_type,
                        'data': base64.b64encode(image_bytes).decode('ascii'),
                    }
                },
            ]
        }],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 80,
        },
    }
    request = Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=6) as response:
            body = json.loads(response.read())
        candidates = body.get('candidates') or []
        parts = ((candidates[0].get('content') or {}).get('parts') or []) if candidates else []
        raw_text = ' '.join(str(part.get('text', '')) for part in parts if isinstance(part, dict))
        raw_tokens = re.split(r'[,\n;|]+', raw_text)
        return clean_tags([token.strip(' .*-#"\'') for token in raw_tokens if token.strip()])[:MAX_AI_TAGS]
    except (URLError, ValueError, KeyError, IndexError, OSError) as error:
        LOGGER.warning('Gemini free-tier tagger skipped/fell back to local analyzer: %s', error)
        return []


def _parse_header_dimensions(raw_bytes: bytes) -> tuple[int, int] | None:
    """Extract (width, height) from PNG, GIF, or JPEG header bytes without external libraries."""
    if len(raw_bytes) >= 24 and raw_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        width, height = struct.unpack('>II', raw_bytes[16:24])
        return (width, height) if width > 0 and height > 0 else None
    if len(raw_bytes) >= 10 and raw_bytes[:6] in (b'GIF87a', b'GIF89a'):
        width, height = struct.unpack('<HH', raw_bytes[6:10])
        return (width, height) if width > 0 and height > 0 else None
    return None


def _analyze_local_visual_tags(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Generate zero-cost visual & metadata tags in-process (Pillow pixel analysis + title/format heuristics).

    Guarantees at least `MIN_AI_TAGS` (5) relevant tags at $0.00 cost even without an external API key.
    """
    tags: list[str] = []
    ext = str(row.get('ext', 'jpg')).lower()
    if ext == 'gif':
        tags.append('animated')

    # 1. Extract meaningful semantic words from the photo's title (ignoring camera prefixes and numbers).
    title = str(row.get('title', ''))
    words = [
        word.lower() for word in re.findall(r'[A-Za-z]{3,20}', title)
        if word.lower() not in STOP_WORDS and not word.lower().startswith(('pxl', 'dsc', 'img', 'lmp'))
    ]
    for word in words[:3]:
        tags.append(word)

    # 2. Pixel-level color palette, luminance, contrast and geometry analysis when Pillow is available.
    source_path: Path | None = thumb_path(row)
    if source_path is None:
        candidate = PHOTO_DIR / f'{row["id"]}.{row["ext"]}'
        if candidate.is_file():
            source_path = candidate

    if HAS_PILLOW:
        try:
            import io
            stream_source: Any = io.BytesIO(raw_bytes) if raw_bytes is not None else source_path
            if stream_source is not None:
                with Image.open(stream_source) as img:
                    orig_w, orig_h = img.size
                    if orig_w > orig_h * 1.85:
                        tags.append('panoramic')
                    elif orig_w > orig_h * 1.12:
                        tags.append('landscape')
                    elif orig_h > orig_w * 1.12:
                        tags.append('portrait')
                    else:
                        tags.append('square-frame')

                    sample = img.convert('RGB').resize((32, 32), Image.Resampling.BILINEAR)
                    raw_rgb = sample.tobytes()
                    pixels = [
                        (raw_rgb[offset], raw_rgb[offset + 1], raw_rgb[offset + 2])
                        for offset in range(0, len(raw_rgb) - 2, 3)
                    ]
                    total_px = len(pixels) or 1

                    lum_sum = 0.0
                    sat_sum = 0.0
                    warm_count = 0
                    cool_count = 0
                    green_count = 0
                    sky_top_count = 0

                    for idx, (red, green, blue) in enumerate(pixels):
                        lum = (0.299 * red + 0.587 * green + 0.114 * blue) / 255.0
                        lum_sum += lum
                        max_c = max(red, green, blue)
                        min_c = min(red, green, blue)
                        delta = max_c - min_c
                        sat = (delta / max_c) if max_c > 0 else 0.0
                        sat_sum += sat

                        if red > blue + 25 and red >= green:
                            warm_count += 1
                        elif blue > red + 20 and blue >= green - 10:
                            cool_count += 1
                            if idx < total_px // 3 and lum > 0.35:
                                sky_top_count += 1
                        if green > red + 12 and green > blue + 12:
                            green_count += 1

                    mean_lum = lum_sum / total_px
                    mean_sat = sat_sum / total_px

                    if mean_lum < 0.24:
                        tags.append('low-light')
                    elif mean_lum > 0.74:
                        tags.append('bright-light')
                    else:
                        tags.append('natural-light')

                    if mean_sat < 0.08:
                        tags.append('monochrome')
                    elif mean_sat > 0.42:
                        tags.append('vibrant-color')
                    else:
                        tags.append('muted-tones')

                    if sky_top_count > total_px * 0.12:
                        tags.append('sky')
                    if green_count > total_px * 0.14:
                        tags.extend(['foliage', 'nature'])
                    if warm_count > cool_count * 1.35 and warm_count > total_px * 0.2:
                        tags.append('warm-tones')
                    elif cool_count > warm_count * 1.35 and cool_count > total_px * 0.2:
                        tags.append('cool-tones')
                    else:
                        tags.append('balanced-color')
        except Exception:  # noqa: BLE001
            pass

    # 3. Header/format fallback when Pillow is not installed (e.g. minimal host test environment).
    if len(tags) < MIN_AI_TAGS:
        header_bytes = raw_bytes or b''
        if not header_bytes and source_path is not None:
            try:
                with source_path.open('rb') as handle:
                    header_bytes = handle.read(64)
            except OSError:
                header_bytes = b''
        dims = _parse_header_dimensions(header_bytes)
        if dims:
            width, height = dims
            tags.append('landscape' if width >= height else 'portrait')
        tags.append(f'{ext}-format')

    # 4. Top up from BASELINE_PHOTO_TAGS so every image always gets at least MIN_AI_TAGS (5) valid tags.
    for fallback_tag in BASELINE_PHOTO_TAGS:
        if len(clean_tags(tags)) >= MIN_AI_TAGS:
            break
        tags.append(fallback_tag)

    return clean_tags(tags)[:MAX_AI_TAGS]


def generate_ai_tags_for_row(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Generate at least `MIN_AI_TAGS` (5) AI tags for `row` using free-tier Gemini + zero-cost local analysis."""
    compact_bytes, mime_type = _prepare_compact_image_bytes(row, raw_bytes)
    gemini_tags = _query_gemini_free_tier_tags(compact_bytes, mime_type, str(row.get('title', '')))
    local_tags = _analyze_local_visual_tags(row, raw_bytes if raw_bytes is not None else compact_bytes)
    combined = clean_tags([*gemini_tags, *local_tags])
    for fallback_tag in BASELINE_PHOTO_TAGS:
        if len(combined) >= MIN_AI_TAGS:
            break
        combined = clean_tags([*combined, fallback_tag])
    return combined[:MAX_AI_TAGS]


def apply_ai_tags_to_row(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Populate `row['aiTags']` (at least 5 tags) and merge them into `row['tags']` if not already AI-tagged."""
    if has_ai_tags(row):
        return list(row['aiTags'])
    existing_tags = list(row.get('tags') or [])
    generated = generate_ai_tags_for_row(row, raw_bytes=raw_bytes)
    # Ensure at least MIN_AI_TAGS (5) tags are recorded in row['aiTags'] and merged into row['tags'].
    row['aiTags'] = generated
    row['tags'] = clean_tags([*existing_tags, *generated])
    return generated


def tag_untagged_photos(max_images: int = MAX_AI_IMAGES_PER_RUN) -> int:
    """Tag up to `max_images` indexed photos that have files on disk and do not yet have `aiTags` in `photos.json`.

    Called only on admin manual rescan (`POST /api/rescan`). Returns the number of photos newly AI-tagged.
    """
    existing_ids = {row['id'] for row in available_rows() if not has_ai_tags(row)}
    if not existing_ids:
        return 0

    tagged_by_id: dict[str, list[str]] = {}
    with INDEX_LOCK:
        candidate_rows = [dict(row) for row in load_index() if row['id'] in existing_ids and not has_ai_tags(row)]

    for row in candidate_rows[:max_images]:
        generated = generate_ai_tags_for_row(row)
        if len(generated) >= MIN_AI_TAGS:
            tagged_by_id[row['id']] = generated

    if not tagged_by_id:
        return 0

    with INDEX_LOCK:
        rows = load_index()
        newly_tagged = 0
        for row in rows:
            ai_tags = tagged_by_id.get(row['id'])
            if ai_tags and not has_ai_tags(row):
                row['aiTags'] = ai_tags
                row['tags'] = clean_tags([*(row.get('tags') or []), *ai_tags])
                newly_tagged += 1
        if newly_tagged:
            save_index(rows)
    return newly_tagged
