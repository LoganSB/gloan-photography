"""Low-cost / zero-cost AI tag bot for admin uploads and manual folder rescans.

Design goals:
1. Separate tracking of manual vs. AI tags in `photos.json`:
   - `row['tags']` holds ONLY manual / uploaded tags and is never overwritten or modified by the AI tagger.
   - `row['aiTags']` holds ONLY AI-generated tags (at least `MIN_AI_TAGS = 5` per photo, distinct from `row['tags']`).
   - Only triggers AI tagging activity for a photo when `photos.json` has fewer than 5 valid AI tags.
2. Focus on generic classifications, biomes, subject/focus, and visual colors/tones:
   - Examples: `portrait`, `female_subject`, `landscape`, `forest`, `snow`, `red_rock`, `gray`, `mountains`, `lakes`,
     `sky`, `water`, `desert`, `macro`, `night`, `sunset`, `winter`, `fog`, `rocks`, `trees`, `warm`, `cool`, `blue`, `green`.
   - Never guesses geographic locations, cities, states, countries, or park names.
3. Near-zero or zero cloud cost:
   - When `GLOAN_GEMINI_API_KEY` (or `GEMINI_API_KEY`) is set, calls Google AI Studio's free-tier lightweight vision
     model (`gemini-2.5-flash-lite` by default, configurable via `GLOAN_GEMINI_MODEL`).
   - Reuses existing 480px JPEG thumbnails in `data/thumbs/` (via `thumb_path(row)`) before touching originals in
     `data/photos/` so 30-50 MB files are not streamed over FUSE when a 50 KB thumbnail already exists.
   - Caps Gemini output tokens (`maxOutputTokens: 80`) and per-rescan batch size (`MAX_AI_IMAGES_PER_RUN = 25`).
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

# Deprecated tags from earlier revisions (generic filler + old location-guessing tags).
# Excluded from counting toward MIN_AI_TAGS so a manual rescan automatically replaces them with clean
# classification/biome/color tags.
LEGACY_DEPRECATED_TAGS = {
    'photography', 'digital-image', 'visual-study', 'composition', 'still-frame', 'gallery',
    'jpg-format', 'png-format', 'webp-format', 'gif-format', 'balanced-color', 'muted-tones', 'natural-light',
    'backcountry-terrain', 'backcountry-landscape', 'wilderness-location', 'scenic-overlook', 'natural-terrain',
    'outdoor-region', 'mountain-country', 'highland-vista', 'field-location', 'wide-horizon', 'wide-landscape',
    'vertical-canyon-frame', 'daylight-backcountry', 'open-sky-horizon', 'high-plateau-skyline',
    'snow-or-mist-plateau', 'high-elevation-overlook', 'desert-southwest', 'canyon-country', 'sandstone-formation',
    'red-rock-canyon', 'mountain-woodland', 'forested-terrain', 'wilderness-forest', 'alpine-highlands',
    'waterside-location', 'mountain-stream-or-lake', 'night-sky-location', 'twilight-basin', 'sunlit-highlands',
    'bryce-canyon', 'utah-southwest', 'colorado-plateau', 'hoodoo-amphitheater', 'paunsaugunt-plateau',
    'zion-canyon', 'navajo-sandstone-cliffs', 'virgin-river-gorge', 'arches-national-park', 'moab-utah',
    'entrada-sandstone', 'red-rock-desert', 'canyonlands-utah', 'island-in-the-sky', 'desert-mesa',
    'utah-landscape', 'american-west', 'tokyo-japan', 'kanto-region', 'east-asia', 'japanese-garden',
    'waterside-courtyard', 'japan-landscape', 'temple-grounds', 'pacific-island-region', 'intermountain-west',
    'western-roadside', 'high-desert-horizon', 'subterranean-cavern', 'canyon-grotto', 'karst-limestone',
    'rock-alcove', 'underground-chamber', 'sedimentary-strata', 'canyon-wall-geology', 'sandstone-bedding',
    'colorado-plateau-geology', 'backcountry-trail', 'wilderness-ridge', 'mountain-footpath', 'canyon-rim-trail',
    'canyon-rim-overlook', 'high-plateau-vista', 'mountain-viewpoint', 'scenic-promontory', 'mountain-byway',
    'high-country-highway', 'backcountry-corridor', 'western-open-road', 'ranchland-border', 'rural-highlands',
    'western-pasture', 'countryside-boundary', 'alpine-snowfield', 'winter-highlands', 'snow-covered-plateau',
    'high-elevation-winter', 'winter-backcountry', 'alpine-winter-zone', 'frost-highlands', 'cold-desert-plateau',
    'high-country-summer', 'summer-wilderness', 'arid-mountain-summer', 'conifer-woodland', 'ponderosa-forest',
    'subalpine-timberline', 'mountain-forest-slope', 'misty-canyon-rim', 'cloud-inversion-valley',
    'fog-bound-overlook', 'storm-cloud-plateau', 'overcast-highlands', 'diffused-sky-basin',
    'western-horizon-dusk', 'desert-twilight-sky', 'sunset-overlook', 'alpine-dawn-ridge',
    'eastern-horizon-light', 'morning-canyon-rim', 'micro-habitat', 'close-up-field-study', 'natural-surface-detail',
    'eagle-nebula-m16', 'serpens-constellation', 'deep-space-observatory', 'stellar-nursery',
    'deep-space-nebula', 'galactic-emission-cloud', 'night-sky-field', 'astrophotography-target',
    'dark-sky-preserve', 'starfield-region', 'celestial-hemisphere', 'deep-sky-location', 'dark-sky-observatory',
    'galactic-field', 'riparian-basin', 'waterside-habitat', 'reflective-pond', 'botanical-habitat',
    'woodland-understory', 'native-flora-zone',
}

# Generic fallback classification tags used only when a tiny/synthetic image still needs tags to reach MIN_AI_TAGS (5).
CLASSIFICATION_BASELINE_TAGS = (
    'outdoors',
    'nature',
    'scenic',
    'daylight',
    'color',
    'still_life',
    'subject',
)

# Maps generic subject/biome words from a photo's title or uploaded tags to clean classification/biome tags.
# Contains zero geographic or place names.
CLASSIFICATION_CUE_MAP: dict[str, tuple[str, ...]] = {
    'portrait': ('portrait', 'person', 'close_up'),
    'woman': ('portrait', 'female_subject', 'person'),
    'female': ('portrait', 'female_subject', 'person'),
    'girl': ('portrait', 'female_subject', 'person'),
    'man': ('portrait', 'male_subject', 'person'),
    'male': ('portrait', 'male_subject', 'person'),
    'boy': ('portrait', 'male_subject', 'person'),
    'forest': ('forest', 'trees', 'nature'),
    'woods': ('forest', 'trees', 'nature'),
    'pine': ('forest', 'trees', 'evergreen'),
    'trees': ('forest', 'trees', 'nature'),
    'snow': ('snow', 'winter', 'white'),
    'winter': ('winter', 'snow', 'cold'),
    'frost': ('snow', 'winter', 'ice'),
    'fog': ('fog', 'clouds', 'gray'),
    'foggy': ('fog', 'clouds', 'gray'),
    'mist': ('fog', 'mist', 'gray'),
    'overcast': ('overcast', 'clouds', 'gray'),
    'mountain': ('mountains', 'landscape', 'outdoors'),
    'mountains': ('mountains', 'landscape', 'outdoors'),
    'ridge': ('mountains', 'landscape', 'outdoors'),
    'peak': ('mountains', 'landscape', 'outdoors'),
    'lake': ('lakes', 'water', 'landscape'),
    'lakes': ('lakes', 'water', 'landscape'),
    'pond': ('water', 'lakes', 'reflection'),
    'water': ('water', 'lakes', 'nature'),
    'river': ('river', 'water', 'nature'),
    'ocean': ('ocean', 'water', 'coast'),
    'coast': ('coast', 'ocean', 'water'),
    'canyon': ('canyon', 'red_rock', 'rocks'),
    'strata': ('rocks', 'red_rock', 'texture'),
    'cave': ('cave', 'rocks', 'dark'),
    'rock': ('rocks', 'red_rock', 'stone'),
    'desert': ('desert', 'red_rock', 'arid'),
    'sunset': ('sunset', 'sky', 'warm'),
    'sunrise': ('sunrise', 'sky', 'warm'),
    'dusk': ('dusk', 'sky', 'evening'),
    'dawn': ('dawn', 'sky', 'morning'),
    'macro': ('macro', 'close_up', 'detail'),
    'flower': ('flowers', 'macro', 'plants'),
    'plants': ('plants', 'green', 'nature'),
    'road': ('road', 'outdoors', 'landscape'),
    'trail': ('trail', 'outdoors', 'nature'),
    'hike': ('trail', 'outdoors', 'landscape'),
    'fence': ('fence', 'outdoors', 'rural'),
    'stars': ('stars', 'night', 'sky'),
    'nebula': ('nebula', 'space', 'night'),
    'astrophotography': ('space', 'stars', 'night'),
    'm16': ('nebula', 'space', 'stars'),
    'animated': ('animated', 'digital_art'),
}

STOP_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'into', 'over', 'under', 'near', 'photo', 'image',
    'img', 'dsc', 'pxl', 'lmp', 'raw', 'untitled', 'copy', 'edit', 'final', 'new', 'old', 'test',
}


def _valid_ai_tags_list(row: Row) -> list[str]:
    """Return cleaned `row['aiTags']` excluding deprecated legacy tags and any manual `row['tags']`."""
    raw_ai = row.get('aiTags')
    if not isinstance(raw_ai, list):
        return []
    manual_set = set(clean_tags(row.get('tags') or []))
    return [tag for tag in clean_tags(raw_ai) if tag not in LEGACY_DEPRECATED_TAGS and tag not in manual_set]


def has_ai_tags(row: Row) -> bool:
    """True when `row` in photos.json already tracks at least `MIN_AI_TAGS` (5) valid AI tags in `aiTags`."""
    return len(_valid_ai_tags_list(row)) >= MIN_AI_TAGS


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


def _query_gemini_free_tier_tags(
    image_bytes: bytes,
    mime_type: str,
    manual_tags: list[str] | None = None,
) -> list[str]:
    """Call Google Gemini Flash-Lite (`generateContent`) for 6-8 generic classification, biome, focus, and color tags.

    Explicitly avoids guessing geographic locations or place names.
    """
    api_key = gemini_api_key()
    if not api_key or not image_bytes or len(image_bytes) > 4 * 1024 * 1024:
        return []

    model = gemini_model_name()
    # Send the API key in the x-goog-api-key header (never in the URL query string) so error logs cannot leak it.
    url = f'{GEMINI_API_BASE}/{model}:generateContent'
    exclude_hint = (
        f' Do not repeat these existing manual tags: {", ".join(manual_tags[:10])}.'
        if manual_tags else ''
    )

    prompt = (
        'You are classifying photographs for a gallery. Return 6 to 8 concise lowercase tags '
        '(single words or underscore_separated terms) focusing ONLY on generic photo classifications, '
        'primary subject/focus, biomes, weather, and dominant colors — for example: '
        'portrait, female_subject, male_subject, landscape, macro, forest, snow, red_rock, gray, '
        'mountains, lakes, water, desert, canyon, sky, clouds, fog, trees, flowers, sunset, night, '
        'blue, green, orange, monochrome. '
        'Do NOT guess specific geographic locations, cities, states, countries, or park names.'
        f'{exclude_hint} Reply with ONLY a comma-separated list of tags.'
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
        headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key},
        method='POST',
    )
    try:
        with urlopen(request, timeout=6) as response:
            body = json.loads(response.read(65536))
        candidates = body.get('candidates') or []
        parts = ((candidates[0].get('content') or {}).get('parts') or []) if candidates else []
        raw_text = ' '.join(str(part.get('text', '')) for part in parts if isinstance(part, dict))
        raw_tokens = re.split(r'[,\n;|]+', raw_text)
        return clean_tags([token.strip(' .*-#"\'') for token in raw_tokens if token.strip()])[:MAX_AI_TAGS]
    except (URLError, ValueError, KeyError, IndexError, OSError) as error:
        status_code = getattr(error, 'code', None)
        LOGGER.warning('Gemini free-tier tagger skipped/fell back to local analyzer (%s%s)',
                       type(error).__name__, f' HTTP {status_code}' if status_code else '')
        return []


def _parse_header_dimensions(raw_bytes: bytes) -> tuple[int, int] | None:
    """Extract (width, height) from PNG or GIF header bytes without external libraries."""
    if len(raw_bytes) >= 24 and raw_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        width, height = struct.unpack('>II', raw_bytes[16:24])
        return (width, height) if width > 0 and height > 0 else None
    if len(raw_bytes) >= 10 and raw_bytes[:6] in (b'GIF87a', b'GIF89a'):
        width, height = struct.unpack('<HH', raw_bytes[6:10])
        return (width, height) if width > 0 and height > 0 else None
    return None


def _analyze_local_visual_tags(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Generate generic classification, biome, subject, and color tags in-process via Pillow pixel regions.

    Guarantees at least `MIN_AI_TAGS` (5) tags at $0.00 cost, distinct from manual `row['tags']`.
    """
    manual_tags = clean_tags(row.get('tags') or [])
    manual_set = set(manual_tags)
    tags: list[str] = []

    if str(row.get('ext', '')).lower() == 'gif':
        tags.append('animated')

    # 1. Map generic biome/subject cues from manual tags and title tokens (no geographic/location names).
    title = str(row.get('title', ''))
    title_tokens = [
        word.lower() for word in re.findall(r'[A-Za-z0-9]{2,24}', title)
        if word.lower() not in STOP_WORDS and not word.lower().startswith(('pxl', 'dsc', 'img', 'lmp'))
    ]
    cue_tokens: list[str] = []
    for token in [*manual_tags, *title_tokens]:
        for sub_part in re.split(r'[-_]+', token.lower()):
            if sub_part and sub_part not in cue_tokens:
                cue_tokens.append(sub_part)

    for cue in cue_tokens:
        for mapped_tag in CLASSIFICATION_CUE_MAP.get(cue, ()):
            if mapped_tag not in manual_set and mapped_tag not in tags:
                tags.append(mapped_tag)

    # 2. Pixel-level biome, composition, and dominant color analysis when Pillow is available.
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
                    if orig_w > orig_h * 1.15:
                        tags.append('landscape')
                    elif orig_h > orig_w * 1.15:
                        tags.append('portrait')
                    else:
                        tags.append('square')

                    sample = img.convert('RGB').resize((32, 32), Image.Resampling.BILINEAR)
                    raw_rgb = sample.tobytes()
                    pixels = [
                        (raw_rgb[offset], raw_rgb[offset + 1], raw_rgb[offset + 2])
                        for offset in range(0, len(raw_rgb) - 2, 3)
                    ]
                    total_px = len(pixels) or 1

                    lum_sum = 0.0
                    sat_sum = 0.0
                    red_rock_count = 0
                    snow_white_count = 0
                    gray_count = 0
                    forest_green_count = 0
                    sky_blue_top = 0
                    water_blue_bottom = 0
                    warm_count = 0
                    cool_count = 0

                    for idx, (red, green, blue) in enumerate(pixels):
                        lum = (0.299 * red + 0.587 * green + 0.114 * blue) / 255.0
                        lum_sum += lum
                        max_c = max(red, green, blue)
                        min_c = min(red, green, blue)
                        sat = ((max_c - min_c) / max_c) if max_c > 0 else 0.0
                        sat_sum += sat

                        if red > 110 and red > green + 18 and green > blue + 10:
                            red_rock_count += 1
                        if lum > 0.72 and sat < 0.16:
                            snow_white_count += 1
                        elif 0.22 <= lum <= 0.72 and sat < 0.14:
                            gray_count += 1
                        if green > 55 and green >= red - 5 and green > blue + 12:
                            forest_green_count += 1
                        if blue > red + 18 and blue >= green - 10:
                            if idx < total_px // 3 and lum > 0.32:
                                sky_blue_top += 1
                            elif idx >= total_px // 3:
                                water_blue_bottom += 1
                        if red > blue + 22:
                            warm_count += 1
                        elif blue > red + 18:
                            cool_count += 1

                    mean_lum = lum_sum / total_px
                    mean_sat = sat_sum / total_px

                    if red_rock_count > total_px * 0.14:
                        tags.extend(['red_rock', 'rocks', 'orange'])
                    if forest_green_count > total_px * 0.14:
                        tags.extend(['forest', 'trees', 'green'])
                    if snow_white_count > total_px * 0.22:
                        tags.extend(['snow', 'white'])
                    if gray_count > total_px * 0.28:
                        tags.extend(['gray', 'overcast'])
                    if sky_blue_top > total_px * 0.10:
                        tags.extend(['sky', 'blue'])
                    if water_blue_bottom > total_px * 0.12:
                        tags.extend(['water', 'lakes', 'blue'])

                    if mean_lum < 0.22:
                        tags.extend(['night', 'dark'])
                    elif mean_lum > 0.74:
                        tags.append('bright')

                    if mean_sat < 0.08:
                        tags.extend(['monochrome', 'gray'])
                    elif warm_count > cool_count * 1.35 and warm_count > total_px * 0.2:
                        tags.append('warm')
                    elif cool_count > warm_count * 1.35 and cool_count > total_px * 0.2:
                        tags.append('cool')
        except Exception:  # noqa: BLE001
            pass

    # 3. Header fallback when Pillow is not installed on host.
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
            if width > height * 1.15:
                tags.append('landscape')
            elif height > width * 1.15:
                tags.append('portrait')
            else:
                tags.append('square')

    # Filter out duplicates of manual `row['tags']` and deprecated legacy tags.
    filtered = [tag for tag in clean_tags(tags) if tag not in manual_set and tag not in LEGACY_DEPRECATED_TAGS]

    # 4. Top up from CLASSIFICATION_BASELINE_TAGS so every image gets >= MIN_AI_TAGS (5) distinct AI tags.
    for baseline_tag in CLASSIFICATION_BASELINE_TAGS:
        if len(filtered) >= MIN_AI_TAGS:
            break
        if baseline_tag not in manual_set and baseline_tag not in filtered:
            filtered.append(baseline_tag)

    return filtered[:MAX_AI_TAGS]


def generate_ai_tags_for_row(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Generate at least `MIN_AI_TAGS` (5) classification/biome/focus AI tags for `row`, distinct from `row['tags']`."""
    manual_tags = clean_tags(row.get('tags') or [])
    manual_set = set(manual_tags)
    existing_ai = _valid_ai_tags_list(row)

    compact_bytes, mime_type = _prepare_compact_image_bytes(row, raw_bytes)
    gemini_tags = _query_gemini_free_tier_tags(
        compact_bytes,
        mime_type,
        manual_tags=manual_tags,
    )
    local_tags = _analyze_local_visual_tags(row, raw_bytes if raw_bytes is not None else compact_bytes)

    combined = [
        tag for tag in clean_tags([*existing_ai, *gemini_tags, *local_tags])
        if tag not in manual_set and tag not in LEGACY_DEPRECATED_TAGS
    ]
    for baseline_tag in CLASSIFICATION_BASELINE_TAGS:
        if len(combined) >= MIN_AI_TAGS:
            break
        if baseline_tag not in manual_set and baseline_tag not in combined:
            combined.append(baseline_tag)
    return combined[:MAX_AI_TAGS]


def apply_ai_tags_to_row(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Populate `row['aiTags']` with >= 5 AI tags if `len(row['aiTags']) < 5`, leaving `row['tags']` untouched."""
    row['tags'] = clean_tags(row.get('tags') or [])
    if has_ai_tags(row):
        row['aiTags'] = _valid_ai_tags_list(row)
        return list(row['aiTags'])
    generated = generate_ai_tags_for_row(row, raw_bytes=raw_bytes)
    row['aiTags'] = generated
    return generated


def tag_untagged_photos(max_images: int = MAX_AI_IMAGES_PER_RUN) -> int:
    """Populate `row['aiTags']` (at least 5 tags) for any indexed photo whose `aiTags` in `photos.json` has < 5 valid tags.

    Also strips any deprecated legacy tags that were merged into `row['tags']` by earlier revisions so `row['tags']`
    contains strictly manual/uploaded tags and `row['aiTags']` is tracked separately.
    """
    with INDEX_LOCK:
        all_rows = load_index()
        cleaned_legacy_manual = False
        for row in all_rows:
            prev_ai_set = set(row.get('aiTags') or [])
            if prev_ai_set and isinstance(row.get('tags'), list):
                pure_manual = [
                    tag for tag in row['tags']
                    if not (tag in prev_ai_set and tag in LEGACY_DEPRECATED_TAGS)
                ]
                if pure_manual != row['tags']:
                    row['tags'] = pure_manual
                    cleaned_legacy_manual = True
        if cleaned_legacy_manual:
            save_index(all_rows)

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
                newly_tagged += 1
        if newly_tagged:
            save_index(rows)
    return newly_tagged


def clear_all_ai_tags() -> int:
    """Remove `aiTags` from every row in `photos.json` while leaving manual `tags` untouched.

    Returns the number of photos that had AI tags cleared.
    """
    with INDEX_LOCK:
        rows = load_index()
        cleared = 0
        for row in rows:
            if row.get('aiTags'):
                row['aiTags'] = []
                cleared += 1
        if cleared:
            save_index(rows)
    return cleared
