"""Flask HTTP application and routes for the site, static assets, photo files and JSON API."""

import ipaddress
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, unquote

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from gloan.auth import Auth, FAILS, normalize_ip
from gloan.config import (
    DEFAULT_TAG_SUGGESTIONS,
    FILE_RE,
    ID_RE,
    LOCK_SECONDS,
    LOGGER,
    MAX_FAILS,
    MAX_TAG_SUGGESTIONS,
    MAX_UPLOAD,
    MIME,
    PHOTO_DIR,
    ROOT,
    THUMB_DIR,
    THUMB_FILE_RE,
)
from gloan.query import parse_int, query_photos
from gloan.storage import (
    INDEX_LOCK,
    Row,
    bump_index_generation,
    clean_story,
    clean_tags,
    clean_title,
    load_index,
    public_row,
    save_index,
    scan_folder,
    sniff_ext,
)
from gloan.tags import suggest_tags
from gloan.thumbtool import start_thumbnail_job, thumbnail_status


# Strict Content-Security-Policy enabled by moving all CSS and JavaScript out of index.html into /static/.
# - script-src 'self' blocks any injected inline <script>, event-handler attributes, or external scripts.
# - img-src allows 'self' plus blob: (used by URL.createObjectURL upload previews) and data:.
# - style-src allows 'self' and 'unsafe-inline' so JS can update element.style.width/transform for progress bars & card tilt.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class Handler:
    """Runtime auth and proxy configuration shared across Flask request threads.

    Keeping these settings on `Handler` preserves backwards compatibility with CLI startup and tests.
    """

    server_version = 'Gloan/1.0'
    auth: Auth = Auth('debugpassword')
    owner_ips: frozenset[str] = frozenset()  # empty = no address restriction
    proxy_header: str | None = None
    proxy_hops: int = 1
    proxy_any_peer: bool = False


app = Flask('gloan', static_folder=None)


def fail(status: int, message: str) -> Response:
    """Build an uncached JSON error response."""
    response = jsonify({'error': message})
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response


def client_ip() -> str:
    """Address of the requester.

    A proxy header (--proxy-header) is trusted only when the direct peer is loopback, i.e. our own proxy,
    or when --proxy-any-peer says every connection comes from a managed proxy (Cloud Run). Entries are counted
    from the right: each trusted proxy appends one, so earlier entries are client-supplied and ignored.
    """
    peer = normalize_ip(request.remote_addr or '127.0.0.1')
    header_value = request.headers.get(Handler.proxy_header, '') if Handler.proxy_header else ''
    forwarded = [part for part in header_value.split(',') if part.strip()]
    if Handler.proxy_header and len(forwarded) >= Handler.proxy_hops:
        try:
            is_loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            is_loopback = False
        if Handler.proxy_any_peer or is_loopback:
            peer = normalize_ip(forwarded[-Handler.proxy_hops])
    return peer


def can_login() -> bool:
    """True when this request comes from an address allowed to be the owner."""
    return not Handler.owner_ips or client_ip() in Handler.owner_ips


def is_owner() -> bool:
    """True for a valid owner token from an allowed address, so a leaked token is useless elsewhere."""
    header = request.headers.get('Authorization', '')
    return header.startswith('Bearer ') and can_login() and Handler.auth.verify(header[7:])


def require_owner() -> Response | None:
    """Return None when the requester is the authenticated owner, or a 401 Response to abort with."""
    return None if is_owner() else fail(401, 'owner login required')


def read_body(limit: int) -> tuple[bytes | None, Response | None]:
    """Read a bounded request body; require an explicit Content-Length within [0, limit]."""
    raw_length = request.headers.get('Content-Length', '')
    try:
        length = int(raw_length)
    except ValueError:
        return None, fail(411, 'Content-Length required')
    if not 0 <= length <= limit:
        return None, fail(413, 'body too large')
    return request.get_data(cache=False), None


@app.after_request
def apply_security_headers(response: Response) -> Response:
    """Attach security headers and default cache policy to every HTTP response."""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = CONTENT_SECURITY_POLICY
    response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.errorhandler(HTTPException)
def handle_http_exception(error: HTTPException) -> Response:
    """Return JSON errors rather than Flask's default HTML error pages."""
    return fail(error.code or 500, 'not found' if error.code == 404 else (error.description or 'request failed'))


# -- Pages & Static Assets ---------------------------------------------------

@app.get('/')
@app.get('/index.html')
def serve_index() -> Response:
    """Serve the single-page gallery shell."""
    response = send_file(ROOT / 'index.html', mimetype='text/html; charset=utf-8', conditional=True)
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.get('/static/<path:filename>')
def serve_static(filename: str) -> Response:
    """Serve CSS and ES module files from static/ (send_from_directory prevents path traversal)."""
    response = send_from_directory(ROOT / 'static', filename, conditional=True)
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.get('/thumbs/<filename>')
def serve_thumbnail(filename: str) -> Response:
    """Serve a generated 480px JPEG tile from THUMB_DIR."""
    match = THUMB_FILE_RE.match(filename)
    if not match:
        return fail(404, 'not found')
    path = THUMB_DIR / match.group(0)
    if not path.is_file():
        return fail(404, 'not found')
    response = send_file(path, mimetype='image/jpeg', conditional=True, max_age=86400)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@app.get('/photos/<filename>')
def serve_photo(filename: str) -> Response:
    """Stream a full-size photo file with ETag/Range support and immutable caching."""
    match = FILE_RE.match(filename)
    if not match:
        return fail(404, 'not found')
    path = PHOTO_DIR / match.group(0)
    if not path.is_file():
        return fail(404, 'not found')
    response = send_file(path, mimetype=MIME[match.group(2)], conditional=True, max_age=31536000)
    response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


# -- JSON API ----------------------------------------------------------------

@app.get('/api/session')
def api_session() -> Response:
    """Report whether the caller is currently logged in and whether login is allowed from their IP."""
    return jsonify({'owner': is_owner(), 'canLogin': can_login()})


@app.get('/api/photos')
def api_list_photos() -> Response:
    """Return one filtered, sorted page of photos and gallery-wide tag counts."""
    params = parse_qs(request.query_string.decode('utf-8', errors='replace'))
    return jsonify(query_photos(params))


@app.get('/api/tags')
def api_suggest_tags() -> Response:
    """Return ranked, typo-tolerant tag suggestions from the cached TagIndex."""
    params = parse_qs(request.query_string.decode('utf-8', errors='replace'))
    limit = parse_int((params.get('limit') or [None])[0], DEFAULT_TAG_SUGGESTIONS, 1, MAX_TAG_SUGGESTIONS)
    query_text = (params.get('q') or [''])[0]
    return jsonify(suggest_tags(query_text, limit))


@app.get('/api/thumbnails')
def api_thumbnail_status() -> Response:
    """Return background thumbnail tool status (owner only)."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    return jsonify(thumbnail_status())


@app.post('/api/thumbnails')
def api_start_thumbnails() -> Response:
    """Start tools/make_thumbnails.py in the background on explicit owner demand."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    if start_thumbnail_job():
        response = jsonify({'started': True})
        response.status_code = 202
        return response
    return fail(409, 'thumbnail job already running')


@app.post('/api/rescan')
def api_rescan() -> Response:
    """Scan data/photos/ for manually copied images and adopt them into photos.json."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    skipped: list[str] = []
    added = scan_folder(skipped)
    indexed_count = len(load_index())
    LOGGER.info('Scan: added %d, skipped %s, indexed %d', added, skipped, indexed_count)
    return jsonify({'added': added, 'skipped': skipped[:10], 'indexed': indexed_count})


@app.post('/api/login')
def api_login() -> Response:
    """Exchange the owner password for a signed token, locking out an IP after repeated failures."""
    if not can_login():
        return fail(404, 'not found')  # indistinguishable from a route that does not exist
    requester_ip = client_ip()
    count, locked_until = FAILS.get(requester_ip, (0, 0.0))
    if locked_until > time.time():
        return fail(429, 'too many attempts, try again shortly')
    raw_body, body_error = read_body(4096)
    if body_error:
        return body_error
    try:
        payload = json.loads(raw_body or b'')
        given_password = str(payload.get('password', ''))
    except (ValueError, AttributeError):
        return fail(400, 'bad json')
    if Handler.auth.check_password(given_password):
        FAILS.pop(requester_ip, None)
        return jsonify({'token': Handler.auth.issue()})
    count += 1
    FAILS[requester_ip] = (count, time.time() + LOCK_SECONDS if count >= MAX_FAILS else 0.0)
    return fail(401, 'wrong password')


@app.post('/api/photos')
def api_upload_photo() -> Response:
    """Store one uploaded image: raw bytes in the body, URL-encoded title and tags in headers."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    data, body_error = read_body(MAX_UPLOAD)
    if body_error or data is None:
        return body_error or fail(400, 'missing body')
    ext = sniff_ext(data)
    if not ext:
        return fail(415, 'not a supported image (jpeg/png/webp/gif)')
    photo_id = 'p' + secrets.token_hex(8)
    row: Row = {
        'id': photo_id,
        'ext': ext,
        'title': clean_title(unquote(request.headers.get('X-Title', ''))),
        'tags': clean_tags(unquote(request.headers.get('X-Tags', '')).split(',')),
        'story': '',
        'added': int(time.time() * 1000),
    }
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    (PHOTO_DIR / f'{photo_id}.{ext}').write_bytes(data)
    with INDEX_LOCK:
        rows = load_index()
        rows.append(row)
        save_index(rows)
    response = jsonify(public_row(row))
    response.status_code = 201
    return response


@app.patch('/api/photos/<photo_id>')
def api_update_photo(photo_id: str) -> Response:
    """Update a photo's title, tags and story (a missing story key leaves the story unchanged)."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    if not ID_RE.match(photo_id):
        return fail(404, 'no such photo')
    raw_body, body_error = read_body(65536)
    if body_error or raw_body is None:
        return body_error or fail(400, 'bad json')
    try:
        body = json.loads(raw_body)
        title = clean_title(body.get('title', ''))
        tags = clean_tags(body.get('tags', []))
    except (ValueError, AttributeError, TypeError):
        return fail(400, 'bad json')
    with INDEX_LOCK:
        rows = load_index()
        row = next((item for item in rows if item['id'] == photo_id), None)
        if row:
            story = clean_story(body['story']) if 'story' in body else row.get('story', '')
            row.update(title=title, tags=tags, story=story)
            save_index(rows)
    if row:
        return jsonify(public_row(row))
    return fail(404, 'no such photo')


@app.delete('/api/photos/<photo_id>')
def api_delete_photo(photo_id: str) -> Response:
    """Delete a photo's metadata entry, original image file and thumbnail tile."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    if not ID_RE.match(photo_id):
        return fail(404, 'no such photo')
    with INDEX_LOCK:
        rows = load_index()
        row = next((item for item in rows if item['id'] == photo_id), None)
        if row:
            save_index([item for item in rows if item['id'] != photo_id])
    if row:
        (PHOTO_DIR / f'{photo_id}.{row["ext"]}').unlink(missing_ok=True)
        (THUMB_DIR / f'{photo_id}.jpg').unlink(missing_ok=True)
        bump_index_generation()
        return jsonify({'ok': True})
    return fail(404, 'no such photo')
