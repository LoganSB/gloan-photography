"""Low-cost / zero-cost AI tag bot for admin uploads and manual folder rescans.

Design goals:
1. Separate tracking of manual vs. AI tags in `photos.json`:
   - `row['tags']` holds ONLY manual / uploaded tags and is never overwritten or modified by the AI tagger.
   - `row['aiTags']` holds ONLY AI-generated tags (at least `MIN_AI_TAGS = 5` per photo, distinct from `row['tags']`).
   - Only triggers AI tagging activity for a photo when `photos.json` has fewer than 5 AI tags (`len(row['aiTags']) < 5`).
2. Strong emphasis on landscape location, geography, landform, and biome:
   - Combines Google Gemini Free Tier (`gemini-2.5-flash-lite` when `GLOAN_GEMINI_API_KEY` or `GEMINI_API_KEY` is set)
     with a zero-cost local landscape/location visual analyzer (using uploaded tags, title location cues, and Pillow
     regional pixel-palette analysis) so every photo gets at least 5 location- and landscape-focused AI tags at $0.00
     or fractions-of-a-cent cloud cost.
3. Cloud Run & Cloud Storage FUSE cost guards:
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

# Legacy generic filler tags from the first AI tagger revision; stripped if they were previously merged into manual
# `tags` and excluded from counting toward the 5 location-focused AI tags so a rescan upgrades them automatically.
LEGACY_FILLER_TAGS = {
    'photography',
    'digital-image',
    'visual-study',
    'composition',
    'still-frame',
    'gallery',
    'jpg-format',
    'png-format',
    'webp-format',
    'gif-format',
    'balanced-color',
    'muted-tones',
    'natural-light',
}

# Location- and landscape-emphasized baseline tags used to guarantee at least MIN_AI_TAGS (5) per photo at $0.00 cost.
LOCATION_BASELINE_TAGS = (
    'backcountry-terrain',
    'wilderness-location',
    'scenic-overlook',
    'natural-terrain',
    'outdoor-region',
    'mountain-country',
    'highland-vista',
    'field-location',
)

# Maps location/park/landform cues in a photo's title or uploaded tags to geographic and landscape-location AI tags.
LOCATION_CUE_MAP: dict[str, tuple[str, ...]] = {
    'bryce': ('bryce-canyon', 'utah-southwest', 'colorado-plateau', 'hoodoo-amphitheater', 'paunsaugunt-plateau'),
    'zion': ('zion-canyon', 'utah-southwest', 'colorado-plateau', 'navajo-sandstone-cliffs', 'virgin-river-gorge'),
    'zions': ('zion-canyon', 'utah-southwest', 'colorado-plateau', 'navajo-sandstone-cliffs', 'desert-southwest'),
    'arches': ('arches-national-park', 'moab-utah', 'colorado-plateau', 'entrada-sandstone', 'red-rock-desert'),
    'canyonlands': ('canyonlands-utah', 'colorado-plateau', 'island-in-the-sky', 'desert-mesa', 'canyon-country'),
    'utah': ('utah-landscape', 'colorado-plateau', 'desert-southwest', 'canyon-country', 'american-west'),
    'tokyo': ('tokyo-japan', 'kanto-region', 'east-asia', 'japanese-garden', 'waterside-courtyard'),
    'japan': ('japan-landscape', 'east-asia', 'japanese-garden', 'temple-grounds', 'pacific-island-region'),
    'maverik': ('american-west', 'intermountain-west', 'western-roadside', 'high-desert-horizon'),
    'cave': ('subterranean-cavern', 'canyon-grotto', 'karst-limestone', 'rock-alcove', 'underground-chamber'),
    'strata': ('sedimentary-strata', 'canyon-wall-geology', 'sandstone-bedding', 'colorado-plateau-geology'),
    'hike': ('backcountry-trail', 'wilderness-ridge', 'mountain-footpath', 'canyon-rim-trail'),
    'lookout': ('canyon-rim-overlook', 'high-plateau-vista', 'mountain-viewpoint', 'scenic-promontory'),
    'road': ('mountain-byway', 'high-country-highway', 'backcountry-corridor', 'western-open-road'),
    'fence': ('ranchland-border', 'rural-highlands', 'western-pasture', 'countryside-boundary'),
    'snow': ('alpine-snowfield', 'winter-highlands', 'snow-covered-plateau', 'high-elevation-winter'),
    'winter': ('winter-backcountry', 'alpine-winter-zone', 'frost-highlands', 'cold-desert-plateau'),
    'summer': ('high-country-summer', 'summer-wilderness', 'arid-mountain-summer'),
    'forest': ('conifer-woodland', 'ponderosa-forest', 'subalpine-timberline', 'mountain-forest-slope'),
    'foggy': ('misty-canyon-rim', 'cloud-inversion-valley', 'fog-bound-overlook'),
    'overcast': ('storm-cloud-plateau', 'overcast-highlands', 'diffused-sky-basin'),
    'sunset': ('western-horizon-dusk', 'desert-twilight-sky', 'sunset-overlook'),
    'sunrise': ('alpine-dawn-ridge', 'eastern-horizon-light', 'morning-canyon-rim'),
    'macro': ('micro-habitat', 'close-up-field-study', 'natural-surface-detail'),
    'm16': ('eagle-nebula-m16', 'serpens-constellation', 'deep-space-observatory', 'stellar-nursery'),
    'nebula': ('deep-space-nebula', 'galactic-emission-cloud', 'night-sky-field', 'astrophotography-target'),
    'stars': ('dark-sky-preserve', 'starfield-region', 'celestial-hemisphere'),
    'astrophotography': ('deep-sky-location', 'dark-sky-observatory', 'galactic-field'),
    'water': ('riparian-basin', 'waterside-habitat', 'reflective-pond'),
    'plants': ('botanical-habitat', 'woodland-understory', 'native-flora-zone'),
}

STOP_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'into', 'over', 'under', 'near', 'photo', 'image',
    'img', 'dsc', 'pxl', 'lmp', 'raw', 'untitled', 'copy', 'edit', 'final', 'new', 'old', 'test',
}


def _valid_ai_tags_list(row: Row) -> list[str]:
    """Return cleaned `row['aiTags']` excluding legacy generic filler tags and any manual `row['tags']`."""
    raw_ai = row.get('aiTags')
    if not isinstance(raw_ai, list):
        return []
    manual_set = set(clean_tags(row.get('tags') or []))
    return [tag for tag in clean_tags(raw_ai) if tag not in LEGACY_FILLER_TAGS and tag not in manual_set]


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
    title: str = '',
    manual_tags: list[str] | None = None,
) -> list[str]:
    """Call Google Gemini Flash-Lite (`generateContent`) for 6-8 location-emphasized landscape tags.

    Uses the title and any uploaded/manual tags as context cues so the model prioritizes geographic location,
    national park/region, landform, and biome while avoiding duplicating the manual tags.
    """
    api_key = gemini_api_key()
    if not api_key or not image_bytes or len(image_bytes) > 4 * 1024 * 1024:
        return []

    model = gemini_model_name()
    url = f'{GEMINI_API_BASE}/{model}:generateContent?key={api_key}'
    context_parts: list[str] = []
    if title:
        context_parts.append(f'Title: "{title[:80]}"')
    if manual_tags:
        context_parts.append(f'Uploaded manual tags (do not repeat these exact words): {", ".join(manual_tags[:10])}')
    context_hint = (' Context: ' + '; '.join(context_parts) + '.') if context_parts else ''

    prompt = (
        'You are tagging a landscape and nature photography gallery. Return 6 to 8 concise lowercase tags '
        '(single words or hyphenated compounds) with a strong emphasis on the LOCATION and geography of the '
        'landscape (e.g. specific national park, region, state/country, plateau, canyon, mountain range, desert '
        'southwest, colorado-plateau, bryce-canyon, zion-canyon, utah, alpine-woodland, pacific-coast), along '
        f'with landform, biome, weather, and lighting.{context_hint} '
        'Reply with ONLY a comma-separated list of tags.'
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
    """Extract (width, height) from PNG or GIF header bytes without external libraries."""
    if len(raw_bytes) >= 24 and raw_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        width, height = struct.unpack('>II', raw_bytes[16:24])
        return (width, height) if width > 0 and height > 0 else None
    if len(raw_bytes) >= 10 and raw_bytes[:6] in (b'GIF87a', b'GIF89a'):
        width, height = struct.unpack('<HH', raw_bytes[6:10])
        return (width, height) if width > 0 and height > 0 else None
    return None


def _analyze_local_visual_tags(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Infer location- and landscape-focused tags in-process from uploaded tags, title cues and Pillow pixel regions.

    Guarantees at least `MIN_AI_TAGS` (5) location/landscape AI tags at $0.00 cost, distinct from `row['tags']`.
    """
    manual_tags = clean_tags(row.get('tags') or [])
    manual_set = set(manual_tags)
    tags: list[str] = []

    # 1. Infer geographic region, park, landform and biome from uploaded manual tags and title tokens.
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
        for mapped_tag in LOCATION_CUE_MAP.get(cue, ()):
            if mapped_tag not in manual_set and mapped_tag not in tags:
                tags.append(mapped_tag)

    # Include meaningful title location words (if not already in manual_set).
    for word in title_tokens:
        if not word.isdigit() and len(word) >= 3 and word not in manual_set and word not in tags:
            tags.append(word)

    # 2. Regional pixel-palette analysis (top sky band, middle horizon/cliff band, bottom ground/water band).
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
                        tags.append('panoramic-vista')
                    elif orig_w >= orig_h:
                        tags.append('wide-horizon')
                    else:
                        tags.append('vertical-canyon-frame')

                    sample = img.convert('RGB').resize((32, 32), Image.Resampling.BILINEAR)
                    raw_rgb = sample.tobytes()
                    pixels = [
                        (raw_rgb[offset], raw_rgb[offset + 1], raw_rgb[offset + 2])
                        for offset in range(0, len(raw_rgb) - 2, 3)
                    ]
                    total_px = len(pixels) or 1

                    lum_sum = 0.0
                    red_rock_count = 0
                    snow_fog_count = 0
                    forest_green_count = 0
                    sky_blue_top = 0
                    water_blue_bottom = 0

                    for idx, (red, green, blue) in enumerate(pixels):
                        lum = (0.299 * red + 0.587 * green + 0.114 * blue) / 255.0
                        lum_sum += lum
                        max_c = max(red, green, blue)
                        min_c = min(red, green, blue)
                        sat = ((max_c - min_c) / max_c) if max_c > 0 else 0.0

                        # Sandstone / terracotta / red-rock canyon palette (Utah / Desert Southwest)
                        if red > 110 and red > green + 18 and green > blue + 10:
                            red_rock_count += 1
                        # High-brightness, low-saturation snowfield / fog / rim mist
                        if lum > 0.72 and sat < 0.16:
                            snow_fog_count += 1
                        # Conifer / woodland foliage green
                        if green > 55 and green >= red - 5 and green > blue + 12:
                            forest_green_count += 1
                        # Blue sky in upper third vs water in lower two-thirds
                        if blue > red + 18 and blue >= green - 10:
                            if idx < total_px // 3 and lum > 0.32:
                                sky_blue_top += 1
                            elif idx >= total_px // 3:
                                water_blue_bottom += 1

                    mean_lum = lum_sum / total_px

                    if red_rock_count > total_px * 0.14:
                        tags.extend(['red-rock-canyon', 'desert-southwest', 'sandstone-formation', 'canyon-country'])
                    if forest_green_count > total_px * 0.14:
                        tags.extend(['mountain-woodland', 'forested-terrain', 'wilderness-forest'])
                    if snow_fog_count > total_px * 0.22:
                        tags.extend(['alpine-highlands', 'snow-or-mist-plateau', 'high-elevation-overlook'])
                    if sky_blue_top > total_px * 0.10:
                        tags.extend(['open-sky-horizon', 'high-plateau-skyline'])
                    if water_blue_bottom > total_px * 0.12:
                        tags.extend(['waterside-location', 'mountain-stream-or-lake'])

                    if mean_lum < 0.22:
                        tags.extend(['night-sky-location', 'twilight-basin'])
                    elif mean_lum > 0.72:
                        tags.append('sunlit-highlands')
                    else:
                        tags.append('daylight-backcountry')
        except Exception:  # noqa: BLE001
            pass

    # 3. Header/format fallback when Pillow is not installed on host.
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
            tags.append('wide-horizon' if width >= height else 'vertical-canyon-frame')

    # Filter out any tags that duplicate manual `row['tags']` or legacy filler tags.
    filtered = [tag for tag in clean_tags(tags) if tag not in manual_set and tag not in LEGACY_FILLER_TAGS]

    # 4. Top up from LOCATION_BASELINE_TAGS so every image is guaranteed >= MIN_AI_TAGS (5) distinct AI tags.
    for baseline_tag in LOCATION_BASELINE_TAGS:
        if len(filtered) >= MIN_AI_TAGS:
            break
        if baseline_tag not in manual_set and baseline_tag not in filtered:
            filtered.append(baseline_tag)

    return filtered[:MAX_AI_TAGS]


def generate_ai_tags_for_row(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Generate at least `MIN_AI_TAGS` (5) location-focused AI tags for `row`, distinct from manual `row['tags']`."""
    manual_tags = clean_tags(row.get('tags') or [])
    manual_set = set(manual_tags)
    existing_ai = _valid_ai_tags_list(row)

    compact_bytes, mime_type = _prepare_compact_image_bytes(row, raw_bytes)
    gemini_tags = _query_gemini_free_tier_tags(
        compact_bytes,
        mime_type,
        title=str(row.get('title', '')),
        manual_tags=manual_tags,
    )
    local_tags = _analyze_local_visual_tags(row, raw_bytes if raw_bytes is not None else compact_bytes)

    combined = [
        tag for tag in clean_tags([*existing_ai, *gemini_tags, *local_tags])
        if tag not in manual_set and tag not in LEGACY_FILLER_TAGS
    ]
    for baseline_tag in LOCATION_BASELINE_TAGS:
        if len(combined) >= MIN_AI_TAGS:
            break
        if baseline_tag not in manual_set and baseline_tag not in combined:
            combined.append(baseline_tag)
    return combined[:MAX_AI_TAGS]


def apply_ai_tags_to_row(row: Row, raw_bytes: bytes | None = None) -> list[str]:
    """Populate `row['aiTags']` with >= 5 AI tags if `len(row['aiTags']) < 5`, leaving `row['tags']` untouched."""
    # Ensure manual `row['tags']` is cleaned but never mixed with AI tags.
    row['tags'] = clean_tags(row.get('tags') or [])
    if has_ai_tags(row):
        row['aiTags'] = _valid_ai_tags_list(row)
        return list(row['aiTags'])
    generated = generate_ai_tags_for_row(row, raw_bytes=raw_bytes)
    row['aiTags'] = generated
    return generated


def tag_untagged_photos(max_images: int = MAX_AI_IMAGES_PER_RUN) -> int:
    """Populate `row['aiTags']` (at least 5 tags) for any indexed photo whose `aiTags` in `photos.json` has < 5 tags.

    Also strips any legacy filler tags that were merged into `row['tags']` by the initial revision so `row['tags']`
    contains strictly manual/uploaded tags and `row['aiTags']` is tracked separately.
    """
    with INDEX_LOCK:
        all_rows = load_index()
        cleaned_legacy_manual = False
        for row in all_rows:
            prev_ai_set = set(row.get('aiTags') or [])
            if prev_ai_set and isinstance(row.get('tags'), list):
                # Remove only tags that were in `aiTags` AND in `LEGACY_FILLER_TAGS` (or auto-appended at the tail).
                pure_manual = [
                    tag for tag in row['tags']
                    if not (tag in prev_ai_set and tag in LEGACY_FILLER_TAGS)
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
