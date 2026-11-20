"""HTTP routes: the site, photo files and the JSON API."""

import ipaddress
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from gloan.auth import Auth, FAILS, normalize_ip
from gloan.config import FILE_RE, ID_RE, LOCK_SECONDS, LOGGER, MAX_FAILS, MAX_UPLOAD, MIME, PHOTO_DIR, ROOT, THUMB_DIR, THUMB_FILE_RE
from gloan.query import query_photos
from gloan.thumbtool import start_thumbnail_job, thumbnail_status
from gloan.storage import INDEX_LOCK, Row, clean_story, clean_tags, clean_title, load_index, public_row, save_index, scan_folder, sniff_ext


class Handler(BaseHTTPRequestHandler):
    """HTTP routes for the site, the photo files and the JSON API."""

    server_version = 'Gloan/1.0'
    auth: Auth
    owner_ips: frozenset[str] = frozenset()  # empty = no address restriction
    proxy_header: str | None = None
    proxy_hops = 1
    proxy_any_peer = False

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin  # signature fixed by base class
        LOGGER.info('%s %s', self.address_string(), format % args)

    # -- helpers ------------------------------------------------------------
    def send_bytes(self, status: int, body: bytes, mime: str, cache: str) -> None:
        """Send a complete response with the headers every route shares."""
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj: Any, status: int = 200) -> None:
        """Send obj as uncached JSON."""
        self.send_bytes(status, json.dumps(obj).encode(), 'application/json', 'no-store')

    def fail(self, status: int, msg: str) -> None:
        """Send a JSON error."""
        self.send_json({'error': msg}, status)

    def client_ip(self) -> str:
        """Address of the requester.

        A proxy header (--proxy-header) is trusted only when the direct peer is loopback, i.e. our own proxy,
        or when --proxy-any-peer says every connection comes from a managed proxy (Cloud Run). Entries are counted
        from the right: each trusted proxy appends one, so earlier entries are client-supplied and ignored.
        """
        peer = normalize_ip(self.client_address[0])
        forwarded = [part for part in self.headers.get(self.proxy_header or '', '').split(',') if part.strip()]
        if self.proxy_header and len(forwarded) >= self.proxy_hops and (
                self.proxy_any_peer or ipaddress.ip_address(peer).is_loopback):
            peer = normalize_ip(forwarded[-self.proxy_hops])
        return peer

    def can_login(self) -> bool:
        """True when this request comes from an address allowed to be the owner."""
        return not self.owner_ips or self.client_ip() in self.owner_ips

    def is_owner(self) -> bool:
        """True for a valid owner token from an allowed address, so a leaked token is useless elsewhere."""
        header = self.headers.get('Authorization', '')
        return header.startswith('Bearer ') and self.can_login() and self.auth.verify(header[7:])

    def require_owner(self) -> bool:
        """Reject the request with 401 unless it comes from the owner."""
        owner = self.is_owner()
        if not owner:
            self.fail(401, 'owner login required')
        return owner

    def read_body(self, limit: int) -> bytes | None:
        """Read the request body; on a missing/oversized length, send the error and return None."""
        body = None
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.fail(411, 'Content-Length required')
        else:
            if 0 <= length <= limit:
                body = self.rfile.read(length)
            else:
                self.fail(413, 'body too large')
        return body

    def photo_id(self) -> str | None:
        """Photo id from the last path segment, or None if it is not a well-formed id."""
        pid = urlparse(self.path).path.rsplit('/', 1)[-1]
        return pid if ID_RE.match(pid) else None

    # -- GET ----------------------------------------------------------------
    def do_GET(self) -> None:  # pylint: disable=invalid-name  # name fixed by http.server
        """Serve the page, the photo list, session status and image files."""
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        match = FILE_RE.match(path.removeprefix('/photos/')) if path.startswith('/photos/') else None
        thumb_match = THUMB_FILE_RE.match(path.removeprefix('/thumbs/')) if path.startswith('/thumbs/') else None
        if path in ('/', '/index.html'):
            self.send_file(ROOT / 'index.html', 'text/html; charset=utf-8', 'no-cache')
        elif path == '/api/session':
            self.send_json({'owner': self.is_owner(), 'canLogin': self.can_login()})
        elif path == '/api/thumbnails':
            if self.require_owner():
                self.send_json(thumbnail_status())
        elif path == '/api/photos':
            self.send_json(query_photos(parse_qs(parsed_url.query)))
        elif thumb_match:
            # Same traversal guard as photos: THUMB_FILE_RE allows no slash or dot beyond the extension.
            # Tiles can be regenerated under the same name, so cache for a day rather than forever.
            self.send_file(THUMB_DIR / thumb_match.group(0), 'image/jpeg', 'public, max-age=86400')
        elif match:
            # CAVEAT: FILE_RE is the path-traversal guard - no slash or dot survives it. Ids are never
            # reused, so the bytes behind a URL never change and can be cached forever.
            self.send_file(PHOTO_DIR / match.group(0), MIME[match.group(2)], 'public, max-age=31536000, immutable')
        else:
            self.fail(404, 'not found')

    def send_file(self, path: Path, mime: str, cache: str) -> None:
        """Send a file from disk, or 404."""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.fail(404, 'not found')
        else:
            self.send_bytes(200, data, mime, cache)

    # -- POST ---------------------------------------------------------------
    def do_POST(self) -> None:  # pylint: disable=invalid-name  # name fixed by http.server
        """Route login and upload."""
        path = urlparse(self.path).path
        if path == '/api/login':
            self.login()
        elif path == '/api/photos':
            self.upload()
        elif path == '/api/thumbnails':
            if self.require_owner():
                started = start_thumbnail_job()
                if started:
                    self.send_json({'started': True}, 202)
                else:
                    self.fail(409, 'thumbnail job already running')
        elif path == '/api/rescan':
            if self.require_owner():
                skipped: list[str] = []
                added = scan_folder(skipped)
                LOGGER.info('Scan: added %d, skipped %s, indexed %d', added, skipped, len(load_index()))
                self.send_json({'added': added, 'skipped': skipped[:10], 'indexed': len(load_index())})
        else:
            self.fail(404, 'not found')

    def login(self) -> None:
        """Exchange the owner password for a token, locking out an IP after repeated failures."""
        if not self.can_login():
            self.fail(404, 'not found')  # indistinguishable from a route that does not exist
            return
        ip = self.client_ip()
        count, locked_until = FAILS.get(ip, (0, 0.0))
        raw = None if locked_until > time.time() else self.read_body(4096)
        if locked_until > time.time():
            self.fail(429, 'too many attempts, try again shortly')
        elif raw is not None:
            try:
                given = str(json.loads(raw).get('password', ''))
            except (ValueError, AttributeError):
                self.fail(400, 'bad json')
            else:
                if self.auth.check_password(given):
                    FAILS.pop(ip, None)
                    self.send_json({'token': self.auth.issue()})
                else:
                    count += 1
                    FAILS[ip] = (count, time.time() + LOCK_SECONDS if count >= MAX_FAILS else 0.0)
                    self.fail(401, 'wrong password')

    def upload(self) -> None:
        """Store one image: raw bytes in the body, title and tags in URL-encoded headers."""
        data = self.read_body(MAX_UPLOAD) if self.require_owner() else None
        if data is None:
            return
        ext = sniff_ext(data)
        if not ext:
            self.fail(415, 'not a supported image (jpeg/png/webp/gif)')
            return
        pid = 'p' + secrets.token_hex(8)
        row: Row = {
            'id': pid,
            'ext': ext,
            'title': clean_title(unquote(self.headers.get('X-Title', ''))),
            'tags': clean_tags(unquote(self.headers.get('X-Tags', '')).split(',')),
            'story': '',
            'added': int(time.time() * 1000),
        }
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / f'{pid}.{ext}').write_bytes(data)
        with INDEX_LOCK:
            rows = load_index()
            rows.append(row)
            save_index(rows)
        self.send_json(public_row(row), 201)

    # -- PATCH / DELETE -----------------------------------------------------
    def do_PATCH(self) -> None:  # pylint: disable=invalid-name  # name fixed by http.server
        """Update a photo's title, tags and story (a missing story key leaves the story unchanged)."""
        pid = self.photo_id()
        raw = self.read_body(65536) if self.require_owner() else None
        if raw is None:
            return
        try:
            body = json.loads(raw)
            title, tags = clean_title(body.get('title', '')), clean_tags(body.get('tags', []))
        except (ValueError, AttributeError, TypeError):
            self.fail(400, 'bad json')
            return
        with INDEX_LOCK:
            rows = load_index()
            row = next((r for r in rows if r['id'] == pid), None)
            if row:
                story = clean_story(body['story']) if 'story' in body else row.get('story', '')
                row.update(title=title, tags=tags, story=story)
                save_index(rows)
        if row:
            self.send_json(public_row(row))
        else:
            self.fail(404, 'no such photo')

    def do_DELETE(self) -> None:  # pylint: disable=invalid-name  # name fixed by http.server
        """Delete a photo and its file."""
        if not self.require_owner():
            return
        pid = self.photo_id()
        with INDEX_LOCK:
            rows = load_index()
            row = next((r for r in rows if r['id'] == pid), None)
            if row:
                save_index([r for r in rows if r['id'] != pid])
        if row:
            (PHOTO_DIR / f'{pid}.{row["ext"]}').unlink(missing_ok=True)
            (THUMB_DIR / f'{pid}.jpg').unlink(missing_ok=True)
            self.send_json({'ok': True})
        else:
            self.fail(404, 'no such photo')
