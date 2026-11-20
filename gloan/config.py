"""Paths, limits and regexes shared by every Gloan module."""

import logging
import os
import re
from pathlib import Path


LOGGER = logging.getLogger('gloan')


ROOT = Path(__file__).resolve().parent.parent  # repo root, one level above this package


DATA = Path(os.environ.get('GLOAN_DATA', ROOT / 'data'))


PHOTO_DIR = DATA / 'photos'


INDEX_FILE = DATA / 'photos.json'


# Downscaled tile images live OUTSIDE photos/ so scan_folder never mistakes them for new photos to adopt.
THUMB_DIR = DATA / 'thumbs'


MIN_PASSWORD = 8


MAX_STORY = 5000


MAX_UPLOAD = 25 * 1024 * 1024


DEFAULT_PAGE_SIZE = 10


MAX_PAGE_SIZE = 100  # caps one response so a client cannot ask for the whole gallery at once


MAX_FILTER_TAGS = 30


MAX_TAG_QUERY = 40  # tags are capped at 40 characters, so longer search text cannot match anything anyway


DEFAULT_TAG_SUGGESTIONS = 8


MAX_TAG_SUGGESTIONS = 20


TOKEN_TTL = 12 * 3600


MAX_FAILS = 5


LOCK_SECONDS = 60


ID_RE = re.compile(r'^p[0-9a-f]{16}$')


FILE_RE = re.compile(r'^(p[0-9a-f]{16})\.(jpg|png|webp|gif)$')


THUMB_FILE_RE = re.compile(r'^(p[0-9a-f]{16})\.jpg$')
MIME = {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}
