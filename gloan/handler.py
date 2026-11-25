"""Flask HTTP application and routes for the site, static assets, photo files and JSON API."""

import hmac
import html
import ipaddress
import json
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from gloan.aitags import apply_ai_tags_to_row, clear_all_ai_tags, tag_untagged_photos
from gloan.auth import (
    Auth,
    FAILS,
    FAILS_LOCK,
    MAX_TRACKED_IPS,
    has_passkey,
    issue_webauthn_challenge,
    load_passkeys,
    normalize_ip,
    register_passkey_credential,
    turnstile_site_key,
    validate_host_header,
    validate_rp_id,
    verify_passkey_assertion,
    verify_turnstile,
)
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
    TOKEN_TTL,
)
from gloan.query import parse_int, query_photos
from gloan.storage import (
    INDEX_LOCK,
    Row,
    bump_index_generation,
    clean_story,
    clean_tags,
    clean_title,
    compute_aspect_ratio,
    get_rows_cache,
    load_index,
    pending_tag_count,
    public_row,
    save_index,
    scan_folder,
    sniff_ext,
    thumb_path,
    validate_image_bytes,
)
from gloan.tags import suggest_tags
from gloan.thumbtool import start_thumbnail_job, thumbnail_status

MAX_QUERY_STRING_BYTES = 2048
SESSION_COOKIE_NAME = 'gloan_session'
MAX_PENDING_TAGS_PER_PHOTO = 10
MAX_TOTAL_PENDING_TAGS = 200
MAX_SUGGEST_TAGS_PER_REQUEST = 5
SUGGEST_RATE_LIMIT_MAX = 12
SUGGEST_RATE_LIMIT_WINDOW_SECONDS = 60.0
SUGGEST_HITS: dict[str, list[float]] = {}
SUGGEST_LOCK = threading.Lock()


# Strict Content-Security-Policy enabled by moving all CSS and JavaScript out of index.html into /static/.
# - script-src & frame-src allow 'self' plus https://challenges.cloudflare.com for the Cloudflare Turnstile widget.
# - img-src allows 'self' plus blob: (used by URL.createObjectURL upload previews) and data:.
# - style-src allows 'self' and 'unsafe-inline' so JS can update element.style.width/transform for progress bars & card tilt.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://challenges.cloudflare.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data:; "
    "connect-src 'self' https://challenges.cloudflare.com; "
    "frame-src https://challenges.cloudflare.com; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class Handler:
    """Runtime auth and proxy configuration shared across Flask request threads.

    Keeping these settings on `Handler` preserves backwards compatibility with CLI startup and tests.
    """

    server_version = 'Gloan/1.0'
    auth: Auth = Auth()
    proxy_header: str | None = None
    proxy_hops: int = 1
    proxy_any_peer: bool = False
    https_redirect: bool = False


app = Flask('gloan', static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD + 1024 * 1024


def _is_trusted_proxy_peer(peer_ip: str | None = None) -> bool:
    """True when the direct peer is trusted to supply X-Forwarded-* headers (loopback or --proxy-any-peer)."""
    if Handler.proxy_any_peer:
        return True
    peer = normalize_ip(peer_ip or request.remote_addr or '127.0.0.1')
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _forwarded_proto() -> str:
    """Return the lowercase X-Forwarded-Proto scheme when supplied by a trusted proxy, else empty string."""
    if not _is_trusted_proxy_peer():
        return ''
    return (request.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()


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
    if Handler.proxy_header and len(forwarded) >= Handler.proxy_hops and _is_trusted_proxy_peer(peer):
        peer = normalize_ip(forwarded[-Handler.proxy_hops])
    return peer


def _request_is_https() -> bool:
    """True when the request arrived over direct TLS or behind a trusted HTTPS proxy."""
    return bool(request.is_secure or _forwarded_proto() == 'https')


def _set_session_cookie(response: Response, token: str) -> None:
    """Attach the signed owner session token as an HttpOnly, SameSite=Strict cookie (Secure over HTTPS)."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=TOKEN_TTL,
        httponly=True,
        secure=_request_is_https(),
        samesite='Strict',
        path='/',
    )


def _clear_session_cookie(response: Response) -> None:
    """Expire the owner HttpOnly session cookie immediately on logout."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        '',
        max_age=0,
        expires=0,
        httponly=True,
        secure=_request_is_https(),
        samesite='Strict',
        path='/',
    )


def _valid_cookie_session() -> str | None:
    """Return the verified session token from the HttpOnly cookie, or None."""
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME, '')
    if cookie_token and Handler.auth.verify(cookie_token):
        return cookie_token
    return None


def _valid_bearer_session() -> str | None:
    """Return the verified session token from `Authorization: Bearer <token>`, or None."""
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer ') and Handler.auth.verify(header[7:]):
        return header[7:]
    return None


def is_owner() -> bool:
    """True for a valid signed owner HttpOnly session cookie or Bearer token."""
    return _valid_cookie_session() is not None or _valid_bearer_session() is not None


def require_owner() -> Response | None:
    """Return None when the requester is the authenticated owner (plus valid CSRF header for cookie mutations)."""
    bearer_token = _valid_bearer_session()
    if bearer_token is not None:
        return None
    cookie_token = _valid_cookie_session()
    if cookie_token is None:
        return fail(401, 'Admin login required')
    if request.method in ('POST', 'PATCH', 'DELETE'):
        csrf_header = (request.headers.get('X-CSRF-Token') or '').strip()
        if not Handler.auth.verify_csrf(cookie_token, csrf_header):
            return fail(403, 'invalid or missing CSRF token')
    return None


def read_body(limit: int) -> tuple[bytes | None, Response | None]:
    """Read a bounded request body; require an explicit Content-Length within [0, limit].

    Reads exactly `length` bytes (already verified to be `<= limit`) from the WSGI input stream so a client
    cannot send an unbounded body or bypass the size cap.
    """
    raw_length = request.headers.get('Content-Length', '')
    try:
        length = int(raw_length)
    except ValueError:
        return None, fail(411, 'Content-Length required')
    if not 0 <= length <= limit:
        return None, fail(413, 'body too large')
    if length == 0:
        return b'', None
    stream = request.environ.get('wsgi.input')
    data = stream.read(length) if stream is not None else request.get_data(cache=False)
    if len(data) != length:
        return None, fail(400, 'body size mismatch')
    return data, None


def _require_json_content_type() -> Response | None:
    """Require `Content-Type: application/json` on JSON mutating routes to block CORS `no-cors` text/plain CSRF."""
    if request.mimetype != 'application/json':
        return fail(415, 'Content-Type must be application/json')
    return None


@app.before_request
def enforce_request_guards() -> Response | None:
    """Enforce query-string bounds, cross-site CSRF/Origin checks, and optional trusted-proxy HTTPS redirects."""
    if len(request.query_string) > MAX_QUERY_STRING_BYTES:
        return fail(414, 'query string too long')

    # Reject cross-site mutating requests (blocks drive-by no-cors IP lockouts and tag-suggestion spam).
    if request.method in ('POST', 'PATCH', 'DELETE'):
        sec_fetch_site = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if sec_fetch_site == 'cross-site':
            return fail(403, 'cross-site requests are not allowed')
        origin_header = (request.headers.get('Origin') or '').strip()
        if origin_header:
            try:
                expected_host = validate_host_header(request.host or 'localhost').split(':', 1)[0]
            except ValueError:
                return fail(400, 'invalid host header')
            origin_host = (urlparse(origin_header).hostname or '').lower()
            if not origin_host or origin_host != expected_host:
                return fail(403, 'cross-origin requests are not allowed')

    if Handler.https_redirect and request.method in ('GET', 'HEAD') and _forwarded_proto() == 'http':
        try:
            validated_host = validate_host_header(request.host or '')
        except ValueError:
            return fail(400, 'invalid host header')
        safe_path = '/' + request.full_path.lstrip('/')
        if safe_path.endswith('?') and not request.query_string:
            safe_path = safe_path[:-1]
        return redirect(f'https://{validated_host}{safe_path}', code=301)
    return None


@app.after_request
def apply_security_headers(response: Response) -> Response:
    """Attach security headers and default cache policy to every HTTP response."""
    response.headers['Server'] = 'Gloan'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Cross-Origin-Opener-Policy'] = 'same-origin'
    response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Content-Security-Policy'] = CONTENT_SECURITY_POLICY
    response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.errorhandler(Exception)
def handle_exception(error: Exception) -> Response:
    """Return JSON errors rather than Flask's default HTML error pages, without leaking tracebacks."""
    if isinstance(error, HTTPException):
        return fail(error.code or 500, 'not found' if error.code == 404 else (error.description or 'request failed'))
    LOGGER.exception('Unhandled server error')
    return fail(500, 'internal server error')


# -- Pages & Static Assets ---------------------------------------------------

@app.get('/')
@app.get('/index.html')
def serve_index() -> Response:
    """Serve the single-page gallery shell, injecting Open Graph meta tags when `?photo=<id>` is shared."""
    shared_id = (request.args.get('photo') or '').strip()
    if shared_id and ID_RE.match(shared_id):
        row = next((item for item in get_rows_cache().rows if item['id'] == shared_id), None)
        if row:
            try:
                validated_host = validate_host_header(request.host or 'localhost')
            except ValueError:
                validated_host = ''
            if validated_host:
                pub = public_row(row)
                scheme = 'https' if (request.is_secure or _forwarded_proto() == 'https') else 'http'
                origin = f'{scheme}://{validated_host}'
                share_url = f'{origin}/?photo={shared_id}'
                image_url = f'{origin}{pub["thumbUrl"] or pub["url"]}'
                title = pub.get('title') or 'Gloan Photography'
                description = (pub.get('story') or 'Landscape & Macro Photography on Gloan')[:200]
                og_tags = (
                    f'<meta property="og:type" content="website">\n'
                    f'<meta property="og:title" content="{html.escape(title, quote=True)}">\n'
                    f'<meta property="og:description" content="{html.escape(description, quote=True)}">\n'
                    f'<meta property="og:image" content="{html.escape(image_url, quote=True)}">\n'
                    f'<meta property="og:url" content="{html.escape(share_url, quote=True)}">\n'
                    f'<meta name="twitter:card" content="summary_large_image">\n'
                )
                page_html = (ROOT / 'index.html').read_text(encoding='utf-8').replace('</head>', og_tags + '</head>', 1)
                response = Response(page_html, mimetype='text/html; charset=utf-8')
                response.headers['Cache-Control'] = 'no-cache'
                return response
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
    """Serve a generated 480px JPEG tile from THUMB_DIR.

    Checks the in-memory RowsCache (`thumb_names`) before touching the filesystem so 404 probes for non-existent
    thumbnail IDs cost zero Cloud Storage FUSE lookup operations.
    """
    match = THUMB_FILE_RE.match(filename)
    if not match or match.group(0) not in get_rows_cache().thumb_names:
        return fail(404, 'not found')
    path = THUMB_DIR / match.group(0)
    if not path.is_file():
        return fail(404, 'not found')
    response = send_file(path, mimetype='image/jpeg', conditional=True, max_age=86400)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@app.get('/photos/<filename>')
def serve_photo(filename: str) -> Response:
    """Stream a full-size photo file with ETag/Range support and immutable caching.

    Checks the in-memory RowsCache (`photo_sizes`) before touching the filesystem so 404 probes for non-existent
    photo IDs cost zero Cloud Storage FUSE lookup operations.
    """
    match = FILE_RE.match(filename)
    if not match or match.group(0) not in get_rows_cache().photo_sizes:
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
    """Report whether the caller is logged in, passkey count, Turnstile site key, and session-bound CSRF token."""
    passkeys = load_passkeys()
    active_token = _valid_cookie_session() or _valid_bearer_session()
    owner = active_token is not None
    return jsonify({
        'owner': owner,
        'hasPasskey': len(passkeys) > 0,
        'passkeyCount': len(passkeys),
        'turnstileSiteKey': turnstile_site_key(),
        'pendingTagCount': pending_tag_count() if owner else 0,
        'csrfToken': Handler.auth.csrf_token(active_token) if active_token else '',
    })


@app.post('/api/logout')
def api_logout() -> Response:
    """Clear the HttpOnly owner session cookie."""
    cookie_token = _valid_cookie_session()
    if cookie_token is not None:
        csrf_header = (request.headers.get('X-CSRF-Token') or '').strip()
        if not Handler.auth.verify_csrf(cookie_token, csrf_header):
            return fail(403, 'invalid or missing CSRF token')
    response = jsonify({'ok': True})
    _clear_session_cookie(response)
    return response


@app.get('/api/photos')
def api_list_photos() -> Response:
    """Return one filtered, sorted page of photos and gallery-wide tag counts."""
    if len(request.query_string) > MAX_QUERY_STRING_BYTES:
        return fail(414, 'query string too long')
    params = parse_qs(request.query_string.decode('utf-8', errors='replace'))
    return jsonify(query_photos(params))


@app.get('/api/tags')
def api_suggest_tags() -> Response:
    """Return ranked, typo-tolerant tag suggestions from the cached TagIndex."""
    if len(request.query_string) > MAX_QUERY_STRING_BYTES:
        return fail(414, 'query string too long')
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
    """Scan data/photos/ for manually copied images, adopt them into photos.json, and AI-tag any untagged photos."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    skipped: list[str] = []
    added = scan_folder(skipped)
    ai_tagged = tag_untagged_photos()
    indexed_count = len(load_index())
    LOGGER.info('Scan: added %d, aiTagged %d, skipped %s, indexed %d', added, ai_tagged, skipped, indexed_count)
    return jsonify({'added': added, 'aiTagged': ai_tagged, 'skipped': skipped[:10], 'indexed': indexed_count})


@app.delete('/api/aitags')
def api_clear_ai_tags() -> Response:
    """Clear all AI-generated `aiTags` from `photos.json` while preserving manual `tags` (owner only)."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    cleared = clear_all_ai_tags()
    LOGGER.info('Cleared AI tags from %d photo(s)', cleared)
    return jsonify({'cleared': cleared})


@app.get('/api/photos/<photo_id>')
def api_get_photo(photo_id: str) -> Response:
    """Return metadata for a single photo by ID so shared `?photo=<id>` URLs can open directly in the lightbox."""
    if not ID_RE.match(photo_id):
        return fail(404, 'no such photo')
    row = next((item for item in get_rows_cache().rows if item['id'] == photo_id), None)
    if not row:
        return fail(404, 'no such photo')
    return jsonify(public_row(row))


def _check_rate_limit(requester_ip: str) -> Response | None:
    """Return a 429 error response if `requester_ip` is currently locked out after repeated login failures."""
    now = time.time()
    with FAILS_LOCK:
        _, locked_until = FAILS.get(requester_ip, (0, 0.0))
    if locked_until > now:
        return fail(429, 'too many attempts, try again shortly')
    return None


def _record_login_failure(requester_ip: str, message: str = 'authentication failed') -> Response:
    """Increment the failure counter for `requester_ip`, lock out at MAX_FAILS, and return a 401 response."""
    now = time.time()
    with FAILS_LOCK:
        if len(FAILS) >= MAX_TRACKED_IPS and requester_ip not in FAILS:
            expired = [ip for ip, (_, until) in FAILS.items() if until <= now]
            for ip in expired:
                FAILS.pop(ip, None)
            if len(FAILS) >= MAX_TRACKED_IPS:
                FAILS.pop(next(iter(FAILS)), None)
        count, _ = FAILS.get(requester_ip, (0, 0.0))
        count += 1
        FAILS[requester_ip] = (count, now + LOCK_SECONDS if count >= MAX_FAILS else 0.0)
    return fail(401, message)


@app.post('/api/passkey/register/options')
def api_passkey_register_options() -> Response:
    """Issue a WebAuthn registration challenge.

    Allowed when no passkey exists yet (initial first-device bootstrap) OR when the caller is already signed in
    with an existing passkey (to enroll additional devices like a desktop or laptop).
    """
    requester_ip = client_ip()
    lockout_error = _check_rate_limit(requester_ip)
    if lockout_error:
        return lockout_error
    if has_passkey():
        auth_error = require_owner()
        if auth_error:
            return auth_error
    try:
        rp_id = validate_rp_id(request.host or 'localhost')
    except ValueError as error:
        return fail(400, str(error))
    exclude_credentials = [{'id': item['credentialId'], 'type': 'public-key'} for item in load_passkeys()]
    return jsonify({
        'challenge': issue_webauthn_challenge('register'),
        'rp': {'name': 'Gloan Photography', 'id': rp_id},
        'user': {'id': 'gloan-owner', 'name': 'owner', 'displayName': 'Gloan Owner'},
        'excludeCredentials': exclude_credentials,
    })


@app.post('/api/passkey/register')
def api_passkey_register() -> Response:
    """Verify and store the owner's newly created WebAuthn Passkey credential."""
    requester_ip = client_ip()
    lockout_error = _check_rate_limit(requester_ip)
    if lockout_error:
        return lockout_error
    already_has_passkey = has_passkey()
    if already_has_passkey:
        auth_error = require_owner()
        if auth_error:
            return auth_error
    ct_error = _require_json_content_type()
    if ct_error:
        return ct_error
    raw_body, body_error = read_body(16384)
    if body_error or raw_body is None:
        return body_error or fail(400, 'bad json')
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError('expected JSON object')
        if not already_has_passkey and not verify_turnstile(str(payload.get('captchaToken', '')), requester_ip):
            return _record_login_failure(requester_ip, 'captcha verification failed')
        register_passkey_credential(payload, request.host or 'localhost')
    except (ValueError, TypeError) as error:
        return _record_login_failure(requester_ip, str(error))
    with FAILS_LOCK:
        FAILS.pop(requester_ip, None)
    session_token = Handler.auth.issue()
    response = jsonify({
        'ok': True,
        'csrfToken': Handler.auth.csrf_token(session_token),
        'passkeyCount': len(load_passkeys()),
    })
    _set_session_cookie(response, session_token)
    return response


@app.post('/api/passkey/login/options')
def api_passkey_login_options() -> Response:
    """Issue a WebAuthn login challenge and return all registered credentialIds."""
    requester_ip = client_ip()
    lockout_error = _check_rate_limit(requester_ip)
    if lockout_error:
        return lockout_error
    stored = load_passkeys()
    if not stored:
        return fail(404, 'no passkey registered yet')
    try:
        rp_id = validate_rp_id(request.host or 'localhost')
    except ValueError as error:
        return fail(400, str(error))
    return jsonify({
        'challenge': issue_webauthn_challenge('login'),
        'rpId': rp_id,
        'allowCredentials': [{'id': item['credentialId'], 'type': 'public-key'} for item in stored],
    })


@app.post('/api/passkey/login')
def api_passkey_login() -> Response:
    """Verify a WebAuthn Passkey login assertion + Cloudflare Turnstile CAPTCHA and return an owner token."""
    requester_ip = client_ip()
    lockout_error = _check_rate_limit(requester_ip)
    if lockout_error:
        return lockout_error
    ct_error = _require_json_content_type()
    if ct_error:
        return ct_error
    raw_body, body_error = read_body(16384)
    if body_error or raw_body is None:
        return body_error or fail(400, 'bad json')
    try:
        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise ValueError('expected JSON object')
    except ValueError:
        return fail(400, 'bad json')
    if not verify_turnstile(str(payload.get('captchaToken', '')), requester_ip):
        return _record_login_failure(requester_ip, 'captcha verification failed')
    try:
        verify_passkey_assertion(payload, request.host or 'localhost')
    except (ValueError, TypeError) as error:
        return _record_login_failure(requester_ip, str(error))
    with FAILS_LOCK:
        FAILS.pop(requester_ip, None)
    session_token = Handler.auth.issue()
    response = jsonify({
        'ok': True,
        'csrfToken': Handler.auth.csrf_token(session_token),
    })
    _set_session_cookie(response, session_token)
    return response


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
    if not ext or not validate_image_bytes(data, ext):
        return fail(415, 'not a supported image (jpeg/png/webp/gif)')
    photo_id = 'p' + secrets.token_hex(8)
    aspect_ratio = compute_aspect_ratio(data)
    raw_title_hdr = request.headers.get('X-Title', '')[:1024]
    raw_tags_hdr = request.headers.get('X-Tags', '')[:2048]
    row: Row = {
        'id': photo_id,
        'ext': ext,
        'title': clean_title(unquote(raw_title_hdr)),
        'tags': clean_tags(unquote(raw_tags_hdr).split(',')),
        'story': '',
        'added': int(time.time() * 1000),
    }
    if aspect_ratio:
        row['aspectRatio'] = aspect_ratio
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    (PHOTO_DIR / f'{photo_id}.{ext}').write_bytes(data)
    apply_ai_tags_to_row(row, raw_bytes=data)
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
    ct_error = _require_json_content_type()
    if ct_error:
        return ct_error
    raw_body, body_error = read_body(65536)
    if body_error or raw_body is None:
        return body_error or fail(400, 'bad json')
    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            raise ValueError('expected JSON object')
        raw_title = body.get('title', '')
        if not isinstance(raw_title, str):
            raise ValueError('title must be a string')
        if 'story' in body and not isinstance(body['story'], str):
            raise ValueError('story must be a string')
        raw_tags = body.get('tags', [])
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            raise ValueError('tags must be a list of strings')
        has_ai_tags = 'aiTags' in body
        raw_ai_tags = body.get('aiTags', [])
        if has_ai_tags and (not isinstance(raw_ai_tags, list) or not all(isinstance(t, str) for t in raw_ai_tags)):
            raise ValueError('aiTags must be a list of strings')
        title = clean_title(raw_title)
        tags = clean_tags(raw_tags)
        ai_tags = clean_tags(raw_ai_tags) if has_ai_tags else None
    except (ValueError, AttributeError, TypeError):
        return fail(400, 'bad json')
    with INDEX_LOCK:
        rows = load_index()
        row = next((item for item in rows if item['id'] == photo_id), None)
        if row:
            story = clean_story(body['story']) if 'story' in body else row.get('story', '')
            row.update(title=title, tags=tags, story=story)
            if ai_tags is not None:
                row['aiTags'] = ai_tags
            save_index(rows)
    if row:
        return jsonify(public_row(row))
    return fail(404, 'no such photo')


def _check_suggest_rate_limit(requester_ip: str) -> Response | None:
    """Rate-limit public tag suggestions per IP (sliding window, bounded IP table) to prevent spam."""
    now = time.time()
    cutoff = now - SUGGEST_RATE_LIMIT_WINDOW_SECONDS
    with SUGGEST_LOCK:
        if len(SUGGEST_HITS) >= MAX_TRACKED_IPS and requester_ip not in SUGGEST_HITS:
            stale = [ip for ip, stamps in SUGGEST_HITS.items() if not any(ts > cutoff for ts in stamps)]
            for ip in stale:
                SUGGEST_HITS.pop(ip, None)
            if len(SUGGEST_HITS) >= MAX_TRACKED_IPS:
                SUGGEST_HITS.pop(next(iter(SUGGEST_HITS)), None)
        recent = [ts for ts in SUGGEST_HITS.get(requester_ip, []) if ts > cutoff]
        if len(recent) >= SUGGEST_RATE_LIMIT_MAX:
            SUGGEST_HITS[requester_ip] = recent
            return fail(429, 'too many tag suggestions, please wait a moment')
        recent.append(now)
        SUGGEST_HITS[requester_ip] = recent
    return None


@app.post('/api/photos/<photo_id>/suggest-tags')
def api_suggest_photo_tags(photo_id: str) -> Response:
    """Allow public visitors to submit tag suggestions that stay hidden until accepted by an admin."""
    if not ID_RE.match(photo_id):
        return fail(404, 'no such photo')
    requester_ip = client_ip()
    rate_error = _check_suggest_rate_limit(requester_ip)
    if rate_error:
        return rate_error
    ct_error = _require_json_content_type()
    if ct_error:
        return ct_error
    raw_body, body_error = read_body(4096)
    if body_error or raw_body is None:
        return body_error or fail(400, 'bad json')
    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            raise ValueError('expected JSON object')
        if not is_owner() and not verify_turnstile(str(body.get('captchaToken', '')), requester_ip):
            return fail(401, 'captcha verification failed')
        raw_tags = body.get('tags', [])
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.split(',')
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            raise ValueError('tags must be a list of strings or comma-separated string')
        candidate_tags = [
            tag for tag in clean_tags(raw_tags)
            if len(tag) >= 2 and any(ch.isalnum() for ch in tag)
        ][:MAX_SUGGEST_TAGS_PER_REQUEST]
    except (ValueError, TypeError):
        return fail(400, 'bad json')
    if not candidate_tags:
        return fail(400, 'no valid tags provided')

    with INDEX_LOCK:
        rows = load_index()
        row = next((item for item in rows if item['id'] == photo_id), None)
        if not row:
            return fail(404, 'no such photo')
        total_pending = pending_tag_count(rows)
        existing = set(clean_tags(row.get('tags') or [])) | set(clean_tags(row.get('aiTags') or []))
        pending = clean_tags(row.get('pendingTags') or [])
        added: list[str] = []
        queue_full = False
        for tag in candidate_tags:
            if tag in existing or tag in pending:
                continue
            if len(pending) >= MAX_PENDING_TAGS_PER_PHOTO or total_pending >= MAX_TOTAL_PENDING_TAGS:
                queue_full = True
                break
            pending.append(tag)
            added.append(tag)
            total_pending += 1
        if queue_full and not added:
            return fail(429, 'pending suggestion queue is full; please try again later')
        if added:
            row['pendingTags'] = pending
            save_index(rows)
    response = jsonify({'added': added, 'pendingCount': len(pending)})
    response.status_code = 201 if added else 200
    return response


@app.get('/api/tag-suggestions')
def api_list_pending_tag_suggestions() -> Response:
    """List all photos that have unapproved visitor-suggested tags awaiting admin review (owner only)."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    items: list[dict[str, Any]] = []
    total = 0
    for row in get_rows_cache().rows:
        pending = clean_tags(row.get('pendingTags') or [])
        if not pending:
            continue
        thumb = thumb_path(row)
        items.append({
            'photoId': row['id'],
            'title': row.get('title') or 'Untitled',
            'thumbUrl': f'/thumbs/{thumb.name}' if thumb else None,
            'url': f'/photos/{row["id"]}.{row["ext"]}',
            'pendingTags': pending,
        })
        total += len(pending)
    return jsonify({'items': items, 'total': total})


@app.post('/api/photos/<photo_id>/pending-tags')
def api_moderate_pending_tag(photo_id: str) -> Response:
    """Accept or reject one pending visitor tag (or `*` for all pending tags) on a photo (owner only)."""
    auth_error = require_owner()
    if auth_error:
        return auth_error
    if not ID_RE.match(photo_id):
        return fail(404, 'no such photo')
    ct_error = _require_json_content_type()
    if ct_error:
        return ct_error
    raw_body, body_error = read_body(4096)
    if body_error or raw_body is None:
        return body_error or fail(400, 'bad json')
    try:
        body = json.loads(raw_body)
        if not isinstance(body, dict):
            raise ValueError('expected JSON object')
        action = str(body.get('action', '')).strip().lower()
        if action not in ('accept', 'reject'):
            raise ValueError('action must be accept or reject')
        raw_tag = str(body.get('tag', '')).strip()
        if not raw_tag:
            raise ValueError('tag is required')
    except (ValueError, TypeError):
        return fail(400, 'bad json')

    with INDEX_LOCK:
        rows = load_index()
        row = next((item for item in rows if item['id'] == photo_id), None)
        if not row:
            return fail(404, 'no such photo')
        pending = clean_tags(row.get('pendingTags') or [])
        if raw_tag == '*':
            targets = list(pending)
            remaining: list[str] = []
        else:
            cleaned = clean_tags([raw_tag])
            target = cleaned[0] if cleaned else ''
            targets = [t for t in pending if t == target]
            remaining = [t for t in pending if t != target]
        if targets:
            if action == 'accept':
                row['tags'] = clean_tags([*(row.get('tags') or []), *targets])
            if remaining:
                row['pendingTags'] = remaining
            else:
                row.pop('pendingTags', None)
            save_index(rows)
        total_remaining = pending_tag_count(rows)
    return jsonify({
        'photo': public_row(row),
        'pendingTags': remaining,
        'pendingTagCount': total_remaining,
    })


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
