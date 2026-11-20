#!/usr/bin/env python3
"""Generate tile thumbnails for every photo that lacks one, and link them from photos.json.

Runs on its own (python3 tools/make_thumbnails.py) or is started on demand by the logged-in owner through the
website (POST /api/thumbnails, which launches this script as a separate process).

For each photo in photos.json whose image file exists in data/photos/:
  - skip it if its tile already exists (unless --force),
  - otherwise downscale it to fit --size px, save a JPEG to data/thumbs/<id>.jpg,
  - finally record row['thumb'] in photos.json so the server can link tile to original.

Needs Pillow (pip install -r tools/requirements.txt). Only this tool needs it; the web server does not.
Honours GLOAN_DATA like the server, so it works on the same data folder.
"""

import argparse
import sys
from pathlib import Path

# Make `import gloan` work when run as a script from anywhere (the package sits one level above tools/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gloan.config import DATA, PHOTO_DIR, THUMB_DIR  # noqa: E402  (must follow the sys.path tweak)
from gloan.storage import INDEX_LOCK, available_rows, load_index, save_index, thumb_path  # noqa: E402

try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit('Pillow is required: pip install -r tools/requirements.txt')


def make_thumbnail(source: Path, destination: Path, longest_edge_px: int, quality: int) -> None:
    """Write a JPEG tile of `source` to `destination`, shrunk to fit longest_edge_px (never enlarged).

    Written to a temporary name then renamed so the server can never serve a half-written tile.
    """
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)  # honour camera rotation so tiles are not sideways
        image.thumbnail((longest_edge_px, longest_edge_px), Image.Resampling.LANCZOS)
        # JPEG has no transparency or palette: flatten anything else (PNG alpha, GIF palette) to RGB.
        image.convert('RGB').save(destination.with_suffix('.tmp'), 'JPEG', quality=quality, optimize=True)
    destination.with_suffix('.tmp').replace(destination)


def main() -> int:
    """Process every photo that needs a tile; return a process exit code (0 = no failures)."""
    parser = argparse.ArgumentParser(description='Generate gallery tile thumbnails')
    parser.add_argument('--size', type=int, default=480, help='longest edge of a tile in px (default 480)')
    parser.add_argument('--quality', type=int, default=80, help='JPEG quality 1-95 (default 80)')
    parser.add_argument('--force', action='store_true', help='regenerate tiles that already exist')
    args = parser.parse_args()

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    made: dict[str, str] = {}  # photo id -> tile file name
    failed = 0
    rows = available_rows()  # rows whose original file is missing are skipped, as on the website
    print(f'Data folder: {DATA}; {len(rows)} photo(s) with files')
    for row in rows:
        if thumb_path(row) and not args.force:
            continue
        name = f'{row["id"]}.jpg'
        try:
            make_thumbnail(PHOTO_DIR / f'{row["id"]}.{row["ext"]}', THUMB_DIR / name, args.size, args.quality)
        except Exception as error:  # noqa: BLE001  one corrupt image must not stop the whole run
            failed += 1
            print(f'FAILED {row["id"]}: {error}')
        else:
            made[row['id']] = name
            print(f'made   {name}')

    if made:
        # Re-read the index right before writing so edits made by the server during the run are not lost.
        with INDEX_LOCK:
            fresh_rows = load_index()
            for row in fresh_rows:
                if row['id'] in made:
                    row['thumb'] = made[row['id']]
            save_index(fresh_rows)
    print(f'Done: {len(made)} made, {failed} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
