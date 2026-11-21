"""Unit and integration tests for Gloan (Flask API, storage/tag caches, thumbnail tool and ES module syntax)."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from werkzeug.serving import BaseWSGIServer, make_server

# Point GLOAN_DATA at an isolated temporary directory BEFORE importing gloan modules.
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix='gloan_test_'))
os.environ['GLOAN_DATA'] = str(TEST_DATA_DIR)

from gloan.auth import Auth, FAILS, b64url_decode, b64url_encode, normalize_ip  # noqa: E402
from gloan.config import PHOTO_DIR, ROOT, THUMB_DIR  # noqa: E402
from gloan.handler import Handler, app  # noqa: E402
from gloan.storage import available_rows, get_rows_cache, load_index, save_index, sniff_ext  # noqa: E402
from gloan.tags import TagIndex, get_tag_index, search_key  # noqa: E402

# Minimal valid 1x1 JPEG bytes for upload / thumbnail tests.
TINY_JPEG = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00'
    b'\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f'
    b'\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b'
    b'\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01'
    b'\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10'
    b'\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12'
    b'!1A\x06\x13Qa\x07"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19'
    b'\x1a%&\'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94'
    b'\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9'
    b'\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4'
    b'\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00'
    b'\x00?\x00\xfb\xd5\xff\xd9'
)


def tearDownModule() -> None:
    """Clean up the temporary data directory after all tests finish."""
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


class FrontendSyntaxTest(unittest.TestCase):
    """Ensure index.html has no inline script (CSP-clean) and all static/*.js ES modules have valid syntax."""

    @staticmethod
    def assert_valid_javascript_literals(script_source: str, filename: str = '<script>') -> None:
        """Scan JS source to verify single/double quotes never cross line boundaries and brackets balance.

        Runs everywhere without external tools so syntax regressions (like an unescaped newline inside a single-
        or double-quoted string, or unbalanced braces/parens/template literals) are caught even in minimal shells.
        """
        stack: list[tuple[str, int]] = []
        index = 0
        line_number = 1
        length = len(script_source)
        prev_significant = ''
        regex_follows = {'(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}', ';', '\n', ''}

        while index < length:
            char = script_source[index]
            nxt = script_source[index + 1] if index + 1 < length else ''
            if char == '\n':
                line_number += 1
            # Single-line comment
            if char == '/' and nxt == '/':
                end = script_source.find('\n', index + 2)
                index = length if end == -1 else end
                continue
            # Multi-line comment
            if char == '/' and nxt == '*':
                end = script_source.find('*/', index + 2)
                if end == -1:
                    raise AssertionError(f'{filename}: Unclosed /* comment starting on line {line_number}')
                line_number += script_source[index:end + 2].count('\n')
                index = end + 2
                continue
            # Single- or double-quoted string literal (must not contain raw unescaped newline)
            if char in ("'", '"'):
                quote = char
                start_line = line_number
                index += 1
                while index < length and script_source[index] != quote:
                    if script_source[index] == '\\':
                        index += 2
                        continue
                    if script_source[index] in ('\n', '\r'):
                        raise AssertionError(f'{filename}: Unterminated {quote} string literal on line {start_line}')
                    index += 1
                if index >= length:
                    raise AssertionError(f'{filename}: Unclosed {quote} string literal starting on line {start_line}')
                prev_significant = quote
                index += 1
                continue
            # Template literal (`...`)
            if char == '`':
                start_line = line_number
                index += 1
                while index < length and script_source[index] != '`':
                    if script_source[index] == '\\':
                        index += 2
                        continue
                    if script_source[index] == '\n':
                        line_number += 1
                    index += 1
                if index >= length:
                    raise AssertionError(f'{filename}: Unclosed template literal ` starting on line {start_line}')
                prev_significant = '`'
                index += 1
                continue
            # Regex literal (/.../) when preceded by an operator/delimiter token
            if char == '/' and prev_significant in regex_follows:
                start_line = line_number
                index += 1
                in_char_class = False
                while index < length:
                    c = script_source[index]
                    if c == '\\':
                        index += 2
                        continue
                    if c == '[':
                        in_char_class = True
                    elif c == ']':
                        in_char_class = False
                    elif c == '/' and not in_char_class:
                        break
                    elif c in ('\n', '\r'):
                        raise AssertionError(f'{filename}: Unterminated regex literal on line {start_line}')
                    index += 1
                prev_significant = '/'
                index += 1
                continue
            # Brackets / braces / parens
            if char in '([{':
                stack.append((char, line_number))
            elif char in ')]}':
                expected = {')': '(', ']': '[', '}': '{'}[char]
                if not stack or stack[-1][0] != expected:
                    raise AssertionError(f'{filename}: Unmatched {char!r} on line {line_number}')
                stack.pop()
            if not char.isspace():
                prev_significant = char
            index += 1

        if stack:
            opener, start_line = stack[-1]
            raise AssertionError(f'{filename}: Unclosed {opener!r} from line {start_line}')

    def test_index_html_and_static_modules_syntax(self) -> None:
        """Verify index.html has no inline JS, static/*.js modules pass syntax checks, and upload skips thumbnails."""
        html_text = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertIn('<link rel="stylesheet" href="/static/app.css">', html_text)
        self.assertIn('<script type="module" src="/static/app.js"></script>', html_text)
        inline_scripts = [body.strip() for body in re.findall(r'<script[^>]*>(.*?)</script>', html_text, re.DOTALL)]
        self.assertEqual(inline_scripts, [''], 'index.html must not contain inline JS (blocked by CSP script-src self)')

        static_dir = ROOT / 'static'
        self.assertTrue((static_dir / 'app.css').is_file(), 'static/app.css must exist')
        expected_modules = ('api.js', 'gallery.js', 'lightbox.js', 'upload.js', 'search.js', 'app.js')
        js_files = sorted(static_dir.glob('*.js'))
        self.assertEqual({path.name for path in js_files}, set(expected_modules))

        # Upload module must not auto-trigger runThumbnailJob (which would consume Cloud Run CPU on upload).
        upload_source = (static_dir / 'upload.js').read_text(encoding='utf-8')
        self.assertNotIn('runThumbnailJob(', upload_source)

        # Verify instant-response UI loading indicators and emoji-free UI text (except ▶/❚❚ on slideshow).
        self.assertIn('id="lbLoader"', html_text)
        self.assertIn('Loading image…', html_text)
        for forbidden_emoji in ('📖', '🔓', '🔒', '🗑', '✎', '⛶', '＋'):
            self.assertNotIn(forbidden_emoji, html_text)
            for js_file in js_files:
                self.assertNotIn(forbidden_emoji, js_file.read_text(encoding='utf-8'))
        self.assertIn('▶', html_text)
        css_source = (static_dir / 'app.css').read_text(encoding='utf-8')
        self.assertIn('.spinner', css_source)
        self.assertIn('.card-loader', css_source)
        self.assertIn('.lb-loader', css_source)
        gallery_source = (static_dir / 'gallery.js').read_text(encoding='utf-8')
        self.assertIn('card-loader', gallery_source)
        self.assertIn('Loading image…', gallery_source)
        self.assertIn("match: 'all'", gallery_source)
        self.assertIn('TILE_LOAD_CONCURRENCY', gallery_source)
        lightbox_source = (static_dir / 'lightbox.js').read_text(encoding='utf-8')
        self.assertIn("classList.add('loading')", lightbox_source)
        self.assertIn('pauseGalleryImageLoads()', lightbox_source)
        self.assertIn('requestAnimationFrame(', lightbox_source)
        self.assertIn('withBusy(', lightbox_source)
        self.assertIn('width:auto;height:auto;object-fit:contain', css_source)
        search_source = (static_dir / 'search.js').read_text(encoding='utf-8')
        self.assertIn('splitCommaInput(', search_source)
        self.assertIn("event.key === 'Tab'", search_source)
        self.assertIn("completedTerms.join(', ') + ', '", search_source)

        node_candidates = [shutil.which('node'), '/mnt/c/Program Files/nodejs/node.exe', 'C:/Program Files/nodejs/node.exe']
        node_binary = next((candidate for candidate in node_candidates if candidate and Path(candidate).exists()), None)

        all_sources = html_text + '\n' + '\n'.join(p.read_text(encoding='utf-8') for p in js_files)
        defined_ids = set(re.findall(r'\bid="([^"]+)"', all_sources))
        for js_path in js_files:
            source = js_path.read_text(encoding='utf-8')
            self.assert_valid_javascript_literals(source, js_path.name)
            # Every select('#someId').<prop> without a null guard must reference an id defined in index.html or JS templates.
            direct_select_ids = set(re.findall(r"select\('#([A-Za-z0-9_-]+)'\)\.", source))
            missing_ids = direct_select_ids - defined_ids
            self.assertEqual(missing_ids, set(),
                             f'{js_path.name} dereferences select(#id) not defined anywhere: {missing_ids}')
            # Every select('#someId').<prop> without a null guard must reference an id defined in index.html or JS templates.
            direct_select_ids = set(re.findall(r"select\('#([A-Za-z0-9_-]+)'\)\.", source))
            missing_ids = direct_select_ids - defined_ids
            self.assertEqual(missing_ids, set(),
                             f'{js_path.name} dereferences select(#id) not defined anywhere: {missing_ids}')
            if node_binary:
                # Use .mjs extension for the syntax check so Node parses ES module import/export syntax.
                with tempfile.NamedTemporaryFile('w', suffix='.mjs', delete=False, encoding='utf-8') as temp_mjs:
                    temp_mjs.write(source)
                    temp_path = temp_mjs.name
                try:
                    check_arg = temp_path
                    if node_binary.endswith('.exe') and shutil.which('wslpath') and temp_path.startswith('/'):
                        check_arg = subprocess.check_output(['wslpath', '-w', temp_path], text=True).strip()
                    result = subprocess.run([node_binary, '--check', check_arg],
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 0, f'JS syntax error in {js_path.name}:\n{result.stderr}')
                finally:
                    Path(temp_path).unlink(missing_ok=True)


class RowsCacheTest(unittest.TestCase):
    """The in-memory RowsCache is reused until photos.json, photos/ or thumbs/ change."""

    def setUp(self) -> None:
        """Create a fresh data directory with one photo file and its index row."""
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / 'p1111111111111111.jpg').write_bytes(TINY_JPEG)
        save_index([{'id': 'p1111111111111111', 'ext': 'jpg', 'title': 'First', 'tags': ['a'], 'added': 1}])

    def test_rows_cache_reused_and_invalidated(self) -> None:
        """Same RowsCache snapshot while nothing changes; rebuilt after save_index or external file modification."""
        first = get_rows_cache()
        self.assertIs(get_rows_cache(), first)
        self.assertEqual(len(available_rows()), 1)
        self.assertEqual(first.photo_sizes['p1111111111111111.jpg'], len(TINY_JPEG))

        # In-process save bumps generation and rebuilds the cache.
        save_index([{'id': 'p1111111111111111', 'ext': 'jpg', 'title': 'Renamed', 'tags': ['a'], 'added': 1}])
        second = get_rows_cache()
        self.assertIsNot(second, first)
        self.assertEqual(available_rows()[0]['title'], 'Renamed')

        # External modification to photos.json is detected via mtime/size.
        index_file = TEST_DATA_DIR / 'photos.json'
        index_file.write_text(json.dumps([{'id': 'p1111111111111111', 'ext': 'jpg', 'title': 'External Edit',
                                           'tags': ['a'], 'added': 1}]), encoding='utf-8')
        future_ns = time.time_ns() + 5_000_000_000
        os.utime(index_file, ns=(future_ns, future_ns))
        self.assertEqual(available_rows()[0]['title'], 'External Edit')


class TagSearchTest(unittest.TestCase):
    """Typo-tolerant tag matching and ranking (pure functions, no server needed)."""

    def setUp(self) -> None:
        """A small tag set: counts decide popularity order among equally good matches."""
        rows = [
            {'tags': ['mountain', 'sunshine', 'Japan', 'single_subject', 'café']},
            {'tags': ['mountain', 'mount-fuji', 'sunset', 'abc']},
            {'tags': ['mountain', 'sunset']},
        ]
        self.index = TagIndex(rows)

    def top(self, query: str) -> tuple[str, str] | None:
        """(tag, match kind) of the best suggestion for `query`, or None."""
        results = self.index.search(query, 8)
        return (results[0]['tag'], results[0]['match']) if results else None

    def test_counts_and_popular_order(self) -> None:
        """Counts come from the rows; an empty query returns most-used tags first."""
        self.assertEqual(self.index.counts['mountain'], 3)
        self.assertEqual([item['tag'] for item in self.index.search('', 2)], ['mountain', 'sunset'])

    def test_normalisation(self) -> None:
        """Case, accents and space/underscore/hyphen differences do not matter."""
        self.assertEqual(search_key('  Single  Subject '), 'single-subject')
        self.assertEqual(self.top('JAPAN'), ('Japan', 'exact'))
        self.assertEqual(self.top('single subject'), ('single_subject', 'exact'))
        self.assertEqual(self.top('cafe'), ('café', 'exact'))

    def test_prefix_word_and_contains(self) -> None:
        """Autocomplete prefix, start of a later word, and plain substring all match, in that order (alphabetical ties)."""
        self.assertEqual(self.top('mou'), ('mount-fuji', 'prefix'))
        self.assertEqual(self.top('moun'), ('mount-fuji', 'prefix'))
        self.assertEqual(self.top('mounta'), ('mountain', 'prefix'))
        self.assertEqual(self.top('subj'), ('single_subject', 'word'))
        self.assertEqual(self.top('unshi'), ('sunshine', 'contains'))
        kinds = [item['match'] for item in self.index.search('mount', 8)]
        self.assertEqual(kinds, sorted(kinds, key=['exact', 'prefix', 'word', 'contains', 'fuzzy'].index))

    def test_single_character_mistakes(self) -> None:
        """Wrong letter, missing letter, extra letter and swapped neighbours are each forgiven once."""
        for typo in ('mointain', 'mountin', 'mounttain', 'moutnain'):
            with self.subTest(typo=typo):
                self.assertEqual(self.top(typo), ('mountain', 'fuzzy'))
        self.assertIsNone(self.top('mxuntxin'), 'two mistakes must not match')

    def test_short_queries_are_not_fuzzy(self) -> None:
        """Two-letter queries only match exactly/prefix/substring; 'zz' finds nothing."""
        self.assertIsNone(self.top('zz'))

    def test_wildcards(self) -> None:
        """'*' and '?' work shell-style against the whole tag, including prefix wildcards and comma-separated AND terms."""
        self.assertEqual(self.top('*shine'), ('sunshine', 'wildcard'))
        self.assertEqual(self.top('suns?t'), ('sunset', 'wildcard'))

        # Predictive search on "bry" returns bryce_fall first alphabetically so Tab autofills "bryce_fall, ".
        seasonal_index = TagIndex([
            {'tags': ['bryce_summer', 'bryce_winter', 'bryce_fall', 'arches_summer', 'arches_winter']}
        ])
        bry_matches = [item['tag'] for item in seasonal_index.search('bry', 8)]
        self.assertEqual(bry_matches, ['bryce_fall', 'bryce_summer', 'bryce_winter'])
        wint_matches = [item['tag'] for item in seasonal_index.search('*wint', 8)]
        self.assertEqual(wint_matches, ['arches_winter', 'bryce_winter'])

    def test_regex_input_is_literal_and_bounded(self) -> None:
        """Regex syntax in the query is matched literally (no injection) and very long text is harmless."""
        # As regex, 'a..' would match 'abc'; literally it is two typos away, so it must find nothing.
        self.assertIsNone(self.top('a..'))
        self.assertEqual(self.index.search('(((((', 8), [])
        self.assertEqual(self.index.search('m' * 5000, 8), [])

    def test_result_cache_returns_same_object(self) -> None:
        """Repeated queries (even differently spaced/cased) are answered from the per-index result cache."""
        self.assertIs(self.index.search('mount', 8), self.index.search(' MOUNT ', 8))


class TagCacheTest(unittest.TestCase):
    """The shared TagIndex is reused until photos.json or photos/ change."""

    def setUp(self) -> None:
        """Fresh data folder with one photo file and its index row."""
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / 'p1111111111111111.jpg').write_bytes(TINY_JPEG)
        save_index([{'id': 'p1111111111111111', 'ext': 'jpg', 'title': 'One', 'tags': ['alpha'], 'added': 1}])

    def test_cache_reused_then_rebuilt(self) -> None:
        """Same object while nothing changes; a new one after an in-process save or an outside edit."""
        first = get_tag_index()
        self.assertIs(get_tag_index(), first)
        self.assertEqual(first.counts, {'alpha': 1})

        save_index([{'id': 'p1111111111111111', 'ext': 'jpg', 'title': 'One', 'tags': ['beta'], 'added': 1}])
        second = get_tag_index()
        self.assertIsNot(second, first)
        self.assertEqual(second.counts, {'beta': 1})

        # Another process (e.g. tools/make_thumbnails.py) rewriting the file is caught via mtime/size.
        index_file = TEST_DATA_DIR / 'photos.json'
        index_file.write_text(json.dumps([{'id': 'p1111111111111111', 'ext': 'jpg', 'title': 'One',
                                           'tags': ['gamma', 'delta'], 'added': 1}]), encoding='utf-8')
        future_ns = time.time_ns() + 5_000_000_000
        os.utime(index_file, ns=(future_ns, future_ns))
        self.assertEqual(get_tag_index().counts, {'gamma': 1, 'delta': 1})


class ServerAndApiTest(unittest.TestCase):
    """End-to-end tests of the Flask server, static assets, CSP, storage, paging, sorting and auth."""

    server: BaseWSGIServer
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        """Start the Flask WSGI server on an ephemeral localhost port."""
        Handler.auth = Auth()
        cls.server = make_server('127.0.0.1', 0, app, threaded=True)
        port = cls.server.server_port
        cls.base_url = f'http://127.0.0.1:{port}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        """Shut down the test HTTP server."""
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        """Reset the temporary data folder, passkey state and login-failure map before each test."""
        FAILS.clear()
        os.environ.pop('GLOAN_TURNSTILE_SITE_KEY', None)
        os.environ.pop('GLOAN_TURNSTILE_SECRET_KEY', None)
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        Handler.auth = Auth()

    def api_call(self, path: str, method: str = 'GET', body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, Any]:
        """Make an HTTP request to the test server and return (status_code, parsed_json_or_bytes)."""
        request = Request(self.base_url + path, data=body, method=method, headers=headers or {})
        try:
            with urlopen(request) as response:
                raw = response.read()
                content_type = response.headers.get('Content-Type', '')
                return response.status, json.loads(raw) if 'application/json' in content_type else raw
        except HTTPError as error:
            try:
                raw = error.read()
                return error.code, json.loads(raw) if raw.startswith(b'{') else raw
            finally:
                error.close()

    def test_static_files_and_csp_headers(self) -> None:
        """Index page and static ES modules are served with Content-Security-Policy: script-src 'self'."""
        with app.test_client() as client:
            with client.get('/') as index_res:
                self.assertEqual(index_res.status_code, 200)
                self.assertIn("script-src 'self'", index_res.headers.get('Content-Security-Policy', ''))
                self.assertEqual(index_res.headers.get('X-Content-Type-Options'), 'nosniff')

            for asset in ('/static/app.css', '/static/app.js', '/static/api.js', '/static/gallery.js',
                          '/static/lightbox.js', '/static/upload.js', '/static/search.js'):
                with client.get(asset) as asset_res:
                    self.assertEqual(asset_res.status_code, 200, f'Failed to fetch {asset}')

    def test_thumbnail_tool_if_pillow_installed(self) -> None:
        """When Pillow is installed (as inside the Docker containers), test tools/make_thumbnails.py end-to-end."""
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest('Pillow not installed in host Python; runs inside Docker container')
        token = self.login_token()
        status, row = self.api_call(
            '/api/photos', 'POST', TINY_JPEG,
            {'Authorization': f'Bearer {token}', 'Content-Type': 'image/jpeg', 'X-Title': 'ThumbTest', 'X-Tags': 't'})
        self.assertEqual(status, 201)
        result = subprocess.run(
            [sys.executable, str(ROOT / 'tools' / 'make_thumbnails.py')],
            env={**os.environ, 'GLOAN_DATA': str(TEST_DATA_DIR)},
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _, listing = self.api_call('/api/photos')
        self.assertEqual(listing['photos'][0]['thumbUrl'], f'/thumbs/{row["id"]}.jpg')
        thumb_status, thumb_bytes = self.api_call(f'/thumbs/{row["id"]}.jpg')
        self.assertEqual(thumb_status, 200)
        self.assertEqual(sniff_ext(thumb_bytes), 'jpg')

    def login_token(self) -> str:
        """Enroll a real ECDSA P-256 WebAuthn passkey (first-device setup) and return the signed owner bearer token."""
        import hashlib
        import struct

        private_key = ec.generate_private_key(ec.SECP256R1())
        spki_der = private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        credential_id = b64url_encode(b'test-owner-passkey-1')
        rp_id_hash = hashlib.sha256(b'127.0.0.1').digest()

        status, reg_opts = self.api_call('/api/passkey/register/options', 'POST', b'')
        self.assertEqual(status, 200, reg_opts)
        reg_client_data = json.dumps({
            'type': 'webauthn.create',
            'challenge': reg_opts['challenge'],
            'origin': self.base_url,
        }).encode('utf-8')
        reg_auth_data = rp_id_hash + bytes([0x05]) + struct.pack('>I', 1)
        status, reg_reply = self.api_call('/api/passkey/register', 'POST', json.dumps({
            'credentialId': credential_id,
            'publicKey': b64url_encode(spki_der),
            'clientDataJSON': b64url_encode(reg_client_data),
            'authenticatorData': b64url_encode(reg_auth_data),
        }).encode('utf-8'), {'Content-Type': 'application/json'})
        self.assertEqual(status, 200, reg_reply)
        return reg_reply['token']

    def test_unit_helpers(self) -> None:
        """Verify image magic-byte sniffing and IPv6-mapped IPv4 normalization."""
        self.assertEqual(sniff_ext(TINY_JPEG), 'jpg')
        self.assertIsNone(sniff_ext(b'not an image'))
        self.assertEqual(normalize_ip('::ffff:127.0.0.1'), '127.0.0.1')

    def test_upload_paging_sorting_and_missing_file_skip(self) -> None:
        """Uploaded photos page/sort properly, and orphan index entries without image files are skipped."""
        token = self.login_token()
        auth_headers = {'Authorization': f'Bearer {token}'}

        # Upload 3 photos with distinct titles and sizes.
        titles = ['Bravo', 'Alpha', 'Charlie']
        uploaded_ids = []
        for index, title in enumerate(titles):
            payload = TINY_JPEG + (b'\x00' * (index * 100))
            status, row = self.api_call(
                '/api/photos', 'POST', payload,
                {**auth_headers, 'Content-Type': 'image/jpeg', 'X-Title': title, 'X-Tags': f'common,tag{index}'})
            self.assertEqual(status, 201)
            uploaded_ids.append(row['id'])
            time.sleep(0.01)  # ensure distinct `added` timestamps

        # Add an orphan row to photos.json whose file does not exist on disk.
        rows = load_index()
        rows.append({'id': 'p0000000000000000', 'ext': 'jpg', 'title': 'Ghost', 'tags': ['ghost'], 'added': 9999999999999})
        save_index(rows)
        self.assertEqual(len(available_rows()), 3, 'Orphan row without a file must be skipped')

        # Paging: size=2, sort=az -> page 0 has Alpha, Bravo; total=3, pages=2; ghost tag excluded.
        status, page0 = self.api_call('/api/photos?page=0&size=2&sort=az')
        self.assertEqual(status, 200)
        self.assertEqual([photo['title'] for photo in page0['photos']], ['Alpha', 'Bravo'])
        self.assertEqual(page0['total'], 3)
        self.assertEqual(page0['all'], 3)
        self.assertEqual(page0['pages'], 2)
        self.assertFalse(any(tag == 'ghost' for tag, _ in page0['tags']))

        # Tag filter + edit + delete.
        status, filtered = self.api_call('/api/photos?tags=tag1')
        self.assertEqual([photo['title'] for photo in filtered['photos']], ['Alpha'])

        alpha_id = uploaded_ids[1]
        status, updated = self.api_call(
            f'/api/photos/{alpha_id}', 'PATCH',
            json.dumps({'title': 'Alpha Edited', 'tags': ['common', 'newtag'], 'story': 'My story'}).encode(),
            {**auth_headers, 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(updated['title'], 'Alpha Edited')
        self.assertIn('newtag', updated['tags'])

        status, _ = self.api_call(f'/api/photos/{alpha_id}', 'DELETE', headers=auth_headers)
        self.assertEqual(status, 200)
        _, after_delete = self.api_call('/api/photos')
        self.assertEqual(after_delete['total'], 2)

    def test_tag_search_endpoint(self) -> None:
        """GET /api/tags suggests tags (typo-tolerant), clamps the limit, and reflects edits immediately."""
        token = self.login_token()
        status, row = self.api_call(
            '/api/photos', 'POST', TINY_JPEG,
            {'Authorization': f'Bearer {token}', 'Content-Type': 'image/jpeg', 'X-Title': 'T',
             'X-Tags': 'landscape,lake'})
        self.assertEqual(status, 201)
        self.assertEqual(row.get('tags'), ['landscape', 'lake'])
        self.assertGreaterEqual(len(row.get('aiTags', [])), 5)
        status, reply = self.api_call('/api/tags?q=landscpe')
        self.assertEqual(status, 200)
        self.assertEqual(reply['suggestions'][0], {'tag': 'landscape', 'count': 1, 'match': 'fuzzy'})
        _, reply = self.api_call('/api/tags?q=&limit=999')
        self.assertGreaterEqual(len(reply['suggestions']), 7)
        self.assertLessEqual(len(reply['suggestions']), 20)

        # Editing manual tags must update `tags`, keep `aiTags` separate, and invalidate the cached TagIndex right away.
        self.api_call(f'/api/photos/{row["id"]}', 'PATCH', json.dumps({'title': 'T', 'tags': ['river']}).encode(),
                      {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        _, reply = self.api_call('/api/tags?q=rivr')
        self.assertEqual(reply['suggestions'][0]['tag'], 'river')
        _, listing = self.api_call('/api/photos')
        self.assertEqual(listing['photos'][0]['tags'], ['river'])
        self.assertGreaterEqual(len(listing['photos'][0]['aiTags']), 5)
        listed_tag_names = {tag_name for tag_name, _ in listing['tags']}
        self.assertIn('river', listed_tag_names)
        self.assertNotIn('lake', listed_tag_names)

    def test_multi_tag_and_filtering_with_random_dataset(self) -> None:
        """Generate a randomized synthetic photo dataset and verify AND chip filtering, comma-delimited search, and per-comma autocomplete."""
        import random

        rng = random.Random(42)
        parks = ['bryce', 'arches', 'zion', 'canyonlands']
        seasons = ['summer', 'winter', 'fall', 'spring']
        extras = ['macro', 'sunrise', 'sunset', 'water', 'plants']

        # Create 40 synthetic photos with compound tags (e.g. bryce_winter) and extra tags.
        synthetic_rows = []
        for idx in range(40):
            photo_id = f'p{idx:016x}'
            (PHOTO_DIR / f'{photo_id}.jpg').write_bytes(TINY_JPEG)
            park = parks[idx % len(parks)]
            season = seasons[(idx // len(parks)) % len(seasons)]
            compound_tag = f'{park}_{season}'
            chosen_extras = rng.sample(extras, k=2)
            synthetic_rows.append({
                'id': photo_id,
                'ext': 'jpg',
                'title': f'Photo {idx} {compound_tag}',
                'tags': [compound_tag, *chosen_extras],
                'story': '',
                'added': 1700000000000 + idx * 1000,
            })
        save_index(synthetic_rows)

        # 1. Tag chip selection below the search bar uses AND matching by default.
        status, chip_and = self.api_call('/api/photos?tags=bryce_winter,macro&size=100')
        self.assertEqual(status, 200)
        expected_chip_ids = {
            row['id'] for row in synthetic_rows if 'bryce_winter' in row['tags'] and 'macro' in row['tags']
        }
        self.assertEqual({photo['id'] for photo in chip_and['photos']}, expected_chip_ids)

        # 2. Wildcard single-term search: q=*wint finds all bryce_winter, arches_winter, zion_winter, canyonlands_winter photos.
        status, wint_res = self.api_call('/api/photos?q=*wint&size=100')
        self.assertEqual(status, 200)
        self.assertTrue(len(wint_res['photos']) > 0)
        for photo in wint_res['photos']:
            self.assertTrue(any('winter' in tag for tag in photo['tags']), photo['tags'])

        # 3. Comma-separated multi-term search: q=*winter, bryce finds ONLY photos matching BOTH *winter AND bryce.
        status, multi_res = self.api_call('/api/photos?q=*winter,+bryce&size=100')
        self.assertEqual(status, 200)
        expected_bryce_winter = {row['id'] for row in synthetic_rows if 'bryce_winter' in row['tags']}
        self.assertEqual({photo['id'] for photo in multi_res['photos']}, expected_bryce_winter)

        # 4. Predictive autocomplete on each comma segment:
        #    - "bry" suggests "bryce_fall", "bryce_spring", "bryce_summer", "bryce_winter" in alphabetical order.
        #    - "bryce_fall, *wint" suggests completions for the second segment ("*wint") while `matchingAll` intersects all terms.
        _, seg1 = self.api_call('/api/tags?q=bry')
        self.assertEqual([item['tag'] for item in seg1['suggestions']],
                         ['bryce_fall', 'bryce_spring', 'bryce_summer', 'bryce_winter'])
        _, seg2 = self.api_call('/api/tags?q=bryce_fall,+*wint')
        self.assertEqual([item['tag'] for item in seg2['suggestions']],
                         ['arches_winter', 'bryce_winter', 'canyonlands_winter', 'zion_winter'])
        _, intersect = self.api_call('/api/tags?q=*winter,+bryce')
        self.assertEqual([item['tag'] for item in intersect['matchingAll']], ['bryce_winter'])

    def test_auth_and_rescan_guard(self) -> None:
        """Unauthenticated calls to owner routes (upload, rescan, thumbnails) return 401 cleanly."""
        self.assertEqual(self.api_call('/api/photos', 'POST', TINY_JPEG)[0], 401)
        self.assertEqual(self.api_call('/api/rescan', 'POST', b'')[0], 401)
        self.assertEqual(self.api_call('/api/thumbnails', 'GET')[0], 401)
        self.assertEqual(self.api_call('/api/thumbnails', 'POST', b'')[0], 401)

        # Authenticated rescan adopts a hand-copied file and AI-tags it with at least 5 tags tracked separately in aiTags.
        (PHOTO_DIR / 'Copied_Photo.jpg').write_bytes(TINY_JPEG)
        token = self.login_token()
        status, scan_reply = self.api_call('/api/rescan', 'POST', b'', {'Authorization': f'Bearer {token}'})
        self.assertEqual(status, 200)
        self.assertEqual(scan_reply['added'], 1)
        self.assertEqual(scan_reply['aiTagged'], 1)
        saved_rows = load_index()
        self.assertEqual(len(saved_rows), 1)
        self.assertEqual(saved_rows[0].get('tags'), [])
        self.assertGreaterEqual(len(saved_rows[0].get('aiTags', [])), 5)

        # A second rescan skips photos that already have >= 5 aiTags recorded in photos.json (0 redundant work/cost).
        status2, scan_reply2 = self.api_call('/api/rescan', 'POST', b'', {'Authorization': f'Bearer {token}'})
        self.assertEqual(status2, 200)
        self.assertEqual(scan_reply2['added'], 0)
        self.assertEqual(scan_reply2['aiTagged'], 0)

        # Existing photos in photos.json with < 5 aiTags trigger AI tagging on manual admin rescan while preserving manual tags.
        (PHOTO_DIR / 'p2222222222222222.jpg').write_bytes(TINY_JPEG)
        rows = load_index()
        rows.append({
            'id': 'p2222222222222222',
            'ext': 'jpg',
            'title': 'Bryce Sunset Canyon Trail',
            'tags': ['utah', 'manual-tag'],
            'aiTags': ['partial-ai-tag'],
            'story': '',
            'added': 1700000000000,
        })
        save_index(rows)
        status3, scan_reply3 = self.api_call('/api/rescan', 'POST', b'', {'Authorization': f'Bearer {token}'})
        self.assertEqual(status3, 200)
        self.assertEqual(scan_reply3['aiTagged'], 1)
        untagged_now = next(item for item in load_index() if item['id'] == 'p2222222222222222')
        # Manual tags are tracked separately and never overwritten or polluted by aiTags.
        self.assertEqual(untagged_now['tags'], ['utah', 'manual-tag'])
        self.assertGreaterEqual(len(untagged_now.get('aiTags', [])), 5)
        self.assertIn('partial-ai-tag', untagged_now['aiTags'])
        self.assertIn('bryce-canyon', untagged_now['aiTags'])
        self.assertIn('colorado-plateau', untagged_now['aiTags'])
        # Both manual tags and aiTags are searchable via /api/photos?q= and /api/photos?tags=.
        _, by_manual = self.api_call('/api/photos?tags=manual-tag')
        self.assertEqual([p['id'] for p in by_manual['photos']], ['p2222222222222222'])
        _, by_ai = self.api_call('/api/photos?tags=bryce-canyon')
        self.assertEqual([p['id'] for p in by_ai['photos']], ['p2222222222222222'])

    def test_passkey_webauthn_multi_device_and_turnstile(self) -> None:
        """Verify multi-device WebAuthn Passkey enrollment (phone + desktop + laptop), assertion login, and Turnstile CAPTCHA."""
        import hashlib
        import struct

        rp_id_hash = hashlib.sha256(b'127.0.0.1').digest()

        # 1. Confirm password login route /api/login does not exist at all (404).
        self.assertEqual(self.api_call('/api/login', 'POST', b'{}', {'Content-Type': 'application/json'})[0], 404)

        # 2. First device (Phone) registers when no passkey exists yet.
        phone_key = ec.generate_private_key(ec.SECP256R1())
        phone_spki = phone_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        phone_cred_id = b64url_encode(b'phone-passkey-cred-id-01')

        status, reg_opts1 = self.api_call('/api/passkey/register/options', 'POST', b'')
        self.assertEqual(status, 200)
        reg_client_data1 = json.dumps({
            'type': 'webauthn.create',
            'challenge': reg_opts1['challenge'],
            'origin': self.base_url,
        }).encode('utf-8')
        reg_auth_data1 = rp_id_hash + bytes([0x05]) + struct.pack('>I', 1)
        status, reg_reply1 = self.api_call('/api/passkey/register', 'POST', json.dumps({
            'credentialId': phone_cred_id,
            'publicKey': b64url_encode(phone_spki),
            'clientDataJSON': b64url_encode(reg_client_data1),
            'authenticatorData': b64url_encode(reg_auth_data1),
        }).encode('utf-8'), {'Content-Type': 'application/json'})
        self.assertEqual(status, 200, reg_reply1)
        self.assertEqual(reg_reply1['passkeyCount'], 1)
        owner_token = reg_reply1['token']

        # Once a passkey is registered, unauthenticated callers cannot register another device (401).
        self.assertEqual(self.api_call('/api/passkey/register/options', 'POST', b'')[0], 401)

        # 3. Authenticated owner enrolls a second device (Desktop PC) without overwriting the Phone passkey.
        desktop_key = ec.generate_private_key(ec.SECP256R1())
        desktop_spki = desktop_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
        desktop_cred_id = b64url_encode(b'desktop-passkey-cred-id-02')
        auth_headers = {'Authorization': f'Bearer {owner_token}', 'Content-Type': 'application/json'}

        status, reg_opts2 = self.api_call('/api/passkey/register/options', 'POST', b'', auth_headers)
        self.assertEqual(status, 200)
        self.assertEqual(reg_opts2['excludeCredentials'], [{'id': phone_cred_id, 'type': 'public-key'}])
        reg_client_data2 = json.dumps({
            'type': 'webauthn.create',
            'challenge': reg_opts2['challenge'],
            'origin': self.base_url,
        }).encode('utf-8')
        reg_auth_data2 = rp_id_hash + bytes([0x05]) + struct.pack('>I', 1)
        status, reg_reply2 = self.api_call('/api/passkey/register', 'POST', json.dumps({
            'credentialId': desktop_cred_id,
            'publicKey': b64url_encode(desktop_spki),
            'clientDataJSON': b64url_encode(reg_client_data2),
            'authenticatorData': b64url_encode(reg_auth_data2),
        }).encode('utf-8'), auth_headers)
        self.assertEqual(status, 200, reg_reply2)
        self.assertEqual(reg_reply2['passkeyCount'], 2)

        # 4. Both Phone and Desktop passkeys can now log in independently.
        status, login_opts = self.api_call('/api/passkey/login/options', 'POST', b'')
        self.assertEqual(status, 200)
        allowed_ids = [item['id'] for item in login_opts['allowCredentials']]
        self.assertEqual(allowed_ids, [phone_cred_id, desktop_cred_id])

        for cred_id, priv_key in ((phone_cred_id, phone_key), (desktop_cred_id, desktop_key)):
            status, opts = self.api_call('/api/passkey/login/options', 'POST', b'')
            self.assertEqual(status, 200)
            login_client_data = json.dumps({
                'type': 'webauthn.get',
                'challenge': opts['challenge'],
                'origin': self.base_url,
            }).encode('utf-8')
            login_auth_data = rp_id_hash + bytes([0x05]) + struct.pack('>I', 2)
            signature = priv_key.sign(login_auth_data + hashlib.sha256(login_client_data).digest(),
                                      ec.ECDSA(hashes.SHA256()))
            status, login_reply = self.api_call('/api/passkey/login', 'POST', json.dumps({
                'credentialId': cred_id,
                'clientDataJSON': b64url_encode(login_client_data),
                'authenticatorData': b64url_encode(login_auth_data),
                'signature': b64url_encode(signature),
            }).encode('utf-8'), {'Content-Type': 'application/json'})
            self.assertEqual(status, 200, login_reply)
            _, authed_session = self.api_call('/api/session',
                                              headers={'Authorization': f'Bearer {login_reply["token"]}'})
            self.assertTrue(authed_session['owner'])
            self.assertEqual(authed_session['passkeyCount'], 2)

        # 5. Replaying with a non-advancing signCount (<= 2) or bad signature is rejected with 401.
        status, login_opts2 = self.api_call('/api/passkey/login/options', 'POST', b'')
        replay_client_data = json.dumps({
            'type': 'webauthn.get',
            'challenge': login_opts2['challenge'],
            'origin': self.base_url,
        }).encode('utf-8')
        stale_auth_data = rp_id_hash + bytes([0x05]) + struct.pack('>I', 2)
        stale_sig = phone_key.sign(stale_auth_data + hashlib.sha256(replay_client_data).digest(),
                                   ec.ECDSA(hashes.SHA256()))
        status, _ = self.api_call('/api/passkey/login', 'POST', json.dumps({
            'credentialId': phone_cred_id,
            'clientDataJSON': b64url_encode(replay_client_data),
            'authenticatorData': b64url_encode(stale_auth_data),
            'signature': b64url_encode(stale_sig),
        }).encode('utf-8'), {'Content-Type': 'application/json'})
        self.assertEqual(status, 401)

        # 6. When Cloudflare Turnstile keys are set, missing captchaToken is rejected before checking credentials.
        os.environ['GLOAN_TURNSTILE_SITE_KEY'] = 'site-key-123'
        os.environ['GLOAN_TURNSTILE_SECRET_KEY'] = 'secret-key-456'
        status, err_reply = self.api_call('/api/passkey/login', 'POST', json.dumps({
            'credentialId': phone_cred_id,
            'captchaToken': '',
        }).encode('utf-8'), {'Content-Type': 'application/json'})
        self.assertEqual(status, 401)
        self.assertIn('captcha', err_reply.get('error', ''))

    def test_security_hardening_boundaries(self) -> None:
        """Verify security headers, path traversal blocks, polyglot/type validation, lockout, and WebAuthn origin/crossOrigin checks."""
        # 1. Defense-in-depth HTTP headers on responses.
        with app.test_client() as client:
            with client.get('/') as res:
                self.assertEqual(res.headers.get('X-Frame-Options'), 'DENY')
                self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff')
                self.assertEqual(res.headers.get('Cross-Origin-Opener-Policy'), 'same-origin')
                self.assertIn('max-age=31536000', res.headers.get('Strict-Transport-Security', ''))

        # 2. Path traversal attempts on /photos/, /thumbs/, and /static/ must return 404.
        for bad_path in ('/photos/..%2fphotos.json', '/photos/../photos.json', '/thumbs/..%2fpasskey.json',
                         '/static/..%2fserver.py', '/api/photos/../../etc/passwd'):
            status, _ = self.api_call(bad_path)
            self.assertEqual(status, 404, f'Expected 404 for {bad_path}, got {status}')

        # 3. PATCH with string instead of list for `tags` must return 400 (not split a string into single-char tags).
        token = self.login_token()
        auth_headers = {'Authorization': f'Bearer {token}'}
        status, row = self.api_call('/api/photos', 'POST', TINY_JPEG,
                                    {**auth_headers, 'Content-Type': 'image/jpeg', 'X-Title': 'Sec\x00Test'})
        self.assertEqual(status, 201)
        self.assertNotIn('\x00', row['title'])
        bad_patch_status, _ = self.api_call(
            f'/api/photos/{row["id"]}', 'PATCH',
            json.dumps({'title': 'T', 'tags': 'not-a-list'}).encode('utf-8'),
            {**auth_headers, 'Content-Type': 'application/json'},
        )
        self.assertEqual(bad_patch_status, 400)

        # 4. Malformed base64 / non-DER public key on passkey register returns 400 cleanly (never 500).
        bad_reg_status, _ = self.api_call(
            '/api/passkey/register', 'POST',
            json.dumps({
                'credentialId': '!!!not_base64!!!',
                'publicKey': '!!!',
                'clientDataJSON': '!!!',
                'authenticatorData': '!!!',
            }).encode('utf-8'),
            {**auth_headers, 'Content-Type': 'application/json'},
        )
        self.assertEqual(bad_reg_status, 401)

        # 5. Brute-force lockout triggers after MAX_FAILS (5) bad attempts on /api/passkey/login.
        FAILS.clear()
        for _ in range(5):
            st, _ = self.api_call('/api/passkey/login', 'POST',
                                  json.dumps({'credentialId': 'invalid'}).encode('utf-8'),
                                  {'Content-Type': 'application/json'})
            self.assertEqual(st, 401)
        locked_status, _ = self.api_call('/api/passkey/login/options', 'POST', b'')
        self.assertEqual(locked_status, 429)

    def test_cloud_cost_and_resource_protection(self) -> None:
        """Verify Cloud Run / GCS FUSE cost guards: query-string limits, in-memory 404 cache checks, rpId validation, and bootstrap Turnstile."""
        # 1. Oversized query strings (> 2048 bytes) on /api/photos and /api/tags are rejected with 414 before parse_qs.
        huge_qs = 'q=' + ('a' * 2500)
        self.assertEqual(self.api_call(f'/api/photos?{huge_qs}')[0], 414)
        self.assertEqual(self.api_call(f'/api/tags?{huge_qs}')[0], 414)

        # 2. Unindexed photo/thumb files on disk are rejected from memory (RowsCache) if not present in the cached snapshot.
        _ = get_rows_cache()  # prime cache while directory is empty
        self.assertEqual(self.api_call('/photos/p9999999999999999.jpg')[0], 404)
        self.assertEqual(self.api_call('/thumbs/p9999999999999999.jpg')[0], 404)

        # 3. Invalid Host header for WebAuthn rpId is rejected with 400.
        self.assertEqual(
            self.api_call('/api/passkey/register/options', 'POST', b'', {'Host': 'evil..host'})[0],
            400,
        )

        # 4. Initial unauthenticated passkey registration requires Cloudflare Turnstile when Turnstile is configured.
        os.environ['GLOAN_TURNSTILE_SITE_KEY'] = 'site-key-123'
        os.environ['GLOAN_TURNSTILE_SECRET_KEY'] = 'secret-key-456'
        status, err = self.api_call(
            '/api/passkey/register', 'POST',
            json.dumps({'credentialId': 'abc', 'captchaToken': ''}).encode('utf-8'),
            {'Content-Type': 'application/json'},
        )
        self.assertEqual(status, 401)
        self.assertIn('captcha', err.get('error', ''))


if __name__ == '__main__':
    unittest.main()
