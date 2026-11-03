#!/usr/bin/env python3
"""Gloan - tiny photo-gallery backend (standard library only).

Public:  GET  /                  the site
         GET  /api/photos        list of photos
         GET  /photos/<id>.<ext> image files
Owner:   POST   /api/login       {"password": ...} -> {"token": ...}
         POST   /api/photos      raw image body, X-Title / X-Tags headers (URL-encoded)
         PATCH  /api/photos/<id> {"title": ..., "tags": [...]}
         DELETE /api/photos/<id>

The owner password is asked for on startup, or taken from the GLOAN_PASSWORD
environment variable; it is never written to a file.
"""
import argparse
import base64
import contextlib
import getpass
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

LOGGER = logging.getLogger('gloan')

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('GLOAN_DATA', ROOT / 'data'))
PHOTO_DIR = DATA / 'photos'
INDEX_FILE = DATA / 'photos.json'

MIN_PASSWORD = 8
MAX_STORY = 5000
MAX_UPLOAD = 25 * 1024 * 1024
TOKEN_TTL = 12 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 60

ID_RE = re.compile(r'^p[0-9a-f]{16}$')
FILE_RE = re.compile(r'^(p[0-9a-f]{16})\.(jpg|png|webp|gif)$')
MIME = {'jpg': 'image/jpeg', 'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}

INDEX_LOCK = threading.Lock()  # guards photos.json read-modify-write across handler threads
FAILS: dict[str, tuple[int, float]] = {}  # ip -> (failed attempts, locked until)

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


def public_row(row: Row) -> Row:
    """Row as the API exposes it, with the image URL filled in."""
    return {'story': '', **row, 'url': f'/photos/{row["id"]}.{row["ext"]}'}


def normalize_ip(raw: str) -> str:
    """Canonical text for an IP so '::ffff:1.2.3.4' and '1.2.3.4' compare equal; junk is returned unchanged."""
    try:
        addr = ipaddress.ip_address(raw.strip())
    except ValueError:
        return raw.strip()
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


class Auth:
    """Stateless owner sessions: an expiry timestamp signed with a password-derived key."""

    def __init__(self, password: str) -> None:
        self.password = password.encode()
        # Derived from the password so changing it logs everyone out.
        self.key = hashlib.sha256(b'gloan-session:' + self.password).digest()

    def check_password(self, given: str) -> bool:
        """Constant-time password comparison (hashed first so lengths do not leak)."""
        return hmac.compare_digest(hashlib.sha256(given.encode()).digest(), hashlib.sha256(self.password).digest())

    def _sig(self, payload: bytes) -> str:
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def issue(self) -> str:
        """Mint a token valid for TOKEN_TTL seconds."""
        payload = str(int(time.time()) + TOKEN_TTL).encode()
        return base64.urlsafe_b64encode(payload).decode() + '.' + self._sig(payload)

    def verify(self, token: str) -> bool:
        """True when the token's signature matches and it has not expired."""
        try:
            b64, sig = token.split('.', 1)
            payload = base64.urlsafe_b64decode(b64.encode())
            ok = hmac.compare_digest(sig, self._sig(payload)) and int(payload) > time.time()
        except ValueError:
            ok = False
        return ok


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
        path = urlparse(self.path).path
        match = FILE_RE.match(path.removeprefix('/photos/')) if path.startswith('/photos/') else None
        if path in ('/', '/index.html'):
            self.send_file(ROOT / 'index.html', 'text/html; charset=utf-8', 'no-cache')
        elif path == '/api/session':
            self.send_json({'owner': self.is_owner(), 'canLogin': self.can_login()})
        elif path == '/api/photos':
            rows = sorted(load_index(), key=lambda r: r['added'], reverse=True)
            self.send_json([public_row(r) for r in rows])
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
            self.send_json({'ok': True})
        else:
            self.fail(404, 'no such photo')


def get_password() -> str:
    """Owner password from GLOAN_PASSWORD if set, otherwise prompted for on the terminal (not echoed)."""
    password = os.environ.get('GLOAN_PASSWORD', '')
    while len(password) < MIN_PASSWORD:
        if not sys.stdin.isatty():
            sys.exit(f'Set GLOAN_PASSWORD to a password of at least {MIN_PASSWORD} characters.')
        if password:
            print(f'That was only {len(password)} characters - please use at least {MIN_PASSWORD}.')
        password = getpass.getpass('Choose an owner password (you will use it to log in on the site): ')
    return password


def stop_on_sigterm(signum: int, frame: Any) -> None:
    """Raise KeyboardInterrupt so serve_forever() unwinds the same way as for Ctrl+C."""
    raise KeyboardInterrupt


def main() -> None:
    """Parse flags, require the owner password, and serve until interrupted."""
    parser = argparse.ArgumentParser(description='Gloan photo gallery server')
    # Loopback by default: exposing the site is a deliberate choice (--host 0.0.0.0 or a reverse proxy).
    parser.add_argument('--host', default='127.0.0.1')
    # Cloud Run (and most PaaS) hand the listening port over in $PORT.
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--owner-ip', action='append', metavar='IP',
                        help='only allow owner login from this address; repeatable (default: any address)')
    parser.add_argument('--proxy-header', metavar='NAME',
                        help='trust this header (e.g. X-Forwarded-For) for the client IP when behind a local proxy')
    parser.add_argument('--proxy-hops', type=int, default=1, metavar='N',
                        help='trusted proxies in front of us; the Nth entry from the right of the header is the client')
    parser.add_argument('--proxy-any-peer', action='store_true',
                        help='trust the proxy header from any peer, not just loopback (only if the container is '
                             'reachable solely through a managed proxy such as Cloud Run)')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    owner_ips = [normalize_ip(ip) for ip in (args.owner_ip or [])]
    for ip in owner_ips:
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            sys.exit(f'--owner-ip {ip!r} is not a valid IP address.')
    Handler.owner_ips = frozenset(owner_ips)
    Handler.proxy_header = args.proxy_header
    Handler.proxy_hops = max(1, args.proxy_hops)
    Handler.proxy_any_peer = args.proxy_any_peer
    Handler.auth = Auth(get_password())
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    adopted = scan_folder()
    if adopted:
        LOGGER.info('Added %d photo(s) found in %s', adopted, PHOTO_DIR)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info('Gloan on http://%s:%s (data: %s)', args.host, args.port, DATA)
    if Handler.owner_ips:
        LOGGER.info('Owner login allowed only from: %s', ', '.join(sorted(Handler.owner_ips)))
    # Cloud Run stops instances with SIGTERM; turn it into the same clean exit as Ctrl+C.
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == '__main__':
    main()
