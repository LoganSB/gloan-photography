"""Unit and integration tests for Gloan (backend API, thumbnail tool, and frontend JavaScript syntax)."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Point GLOAN_DATA at an isolated temporary directory BEFORE importing gloan modules.
TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix='gloan_test_'))
os.environ['GLOAN_DATA'] = str(TEST_DATA_DIR)

from gloan.auth import Auth, FAILS, normalize_ip  # noqa: E402
from gloan.config import PHOTO_DIR, ROOT, THUMB_DIR  # noqa: E402
from gloan.handler import Handler  # noqa: E402
from gloan.storage import available_rows, load_index, save_index, scan_folder, sniff_ext  # noqa: E402

# Minimal valid 1x1 JPEG and 1x1 PNG bytes for upload / thumbnail tests.
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
    """Ensure the embedded <script> in index.html has valid JavaScript syntax and no broken literals."""

    @staticmethod
    def assert_valid_javascript_literals(script_source: str) -> None:
        """Scan JS source to verify single/double quotes never cross line boundaries and brackets balance.

        Runs everywhere without external tools so syntax regressions (like an unescaped newline inside a single-
        or double-quoted string, or unbalanced braces/parens/template literals) are caught even in minimal shells.
        """
        stack: list[tuple[str, int]] = []
        index = 0
        line_number = 1
        length = len(script_source)
        prev_significant = ''
        regex_Follows = {'(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}', ';', '\n', ''}

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
                    raise AssertionError(f'Unclosed /* comment starting on line {line_number}')
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
                        raise AssertionError(f'Unterminated {quote} string literal on line {start_line}')
                    index += 1
                if index >= length:
                    raise AssertionError(f'Unclosed {quote} string literal starting on line {start_line}')
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
                    raise AssertionError(f'Unclosed template literal ` starting on line {start_line}')
                prev_significant = '`'
                index += 1
                continue
            # Regex literal (/.../) when preceded by an operator/delimiter token
            if char == '/' and prev_significant in regex_Follows:
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
                        raise AssertionError(f'Unterminated regex literal on line {start_line}')
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
                    raise AssertionError(f'Unmatched {char!r} on line {line_number}')
                stack.pop()
            if not char.isspace():
                prev_significant = char
            index += 1

        if stack:
            opener, start_line = stack[-1]
            raise AssertionError(f'Unclosed {opener!r} from line {start_line}')

    def test_index_html_javascript_syntax(self) -> None:
        """Extract <script> from index.html and validate JS syntax (built-in scanner + node --check if available)."""
        html_text = (ROOT / 'index.html').read_text(encoding='utf-8')
        match = re.search(r'<script>(.*)</script>', html_text, re.DOTALL)
        self.assertIsNotNone(match, 'index.html must contain a <script> block')
        script_source = match.group(1)

        # Always run the pure-Python lexer check (works inside sandbox and inside the slim Docker image).
        self.assert_valid_javascript_literals(script_source)

        # Also run `node --check` if a Node binary is accessible.
        node_candidates = [shutil.which('node'), '/mnt/c/Program Files/nodejs/node.exe']
        node_binary = next((candidate for candidate in node_candidates if candidate and Path(candidate).exists()), None)
        if node_binary:
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as temp_js:
                temp_js.write(script_source)
                temp_path = temp_js.name
            try:
                check_arg = temp_path
                if node_binary.endswith('.exe') and shutil.which('wslpath'):
                    check_arg = subprocess.check_output(['wslpath', '-w', temp_path], text=True).strip()
                result = subprocess.run([node_binary, '--check', check_arg], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, f'JavaScript syntax error in index.html:\n{result.stderr}')
            finally:
                Path(temp_path).unlink(missing_ok=True)


class ServerAndApiTest(unittest.TestCase):
    """End-to-end tests of the HTTP server, storage, paging, sorting, missing-file skipping, and auth."""

    server: ThreadingHTTPServer
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        """Start the HTTP server on an ephemeral localhost port."""
        Handler.auth = Auth('debugpassword')
        Handler.owner_ips = frozenset()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        port = cls.server.server_address[1]
        cls.base_url = f'http://127.0.0.1:{port}'
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        """Shut down the test HTTP server."""
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        """Reset the temporary data folder and login-failure map before each test."""
        FAILS.clear()
        shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

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
            ['python3', str(ROOT / 'tools' / 'make_thumbnails.py')],
            env={**os.environ, 'GLOAN_DATA': str(TEST_DATA_DIR)},
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _, listing = self.api_call('/api/photos')
        self.assertEqual(listing['photos'][0]['thumbUrl'], f'/thumbs/{row["id"]}.jpg')
        thumb_status, thumb_bytes = self.api_call(f'/thumbs/{row["id"]}.jpg')
        self.assertEqual(thumb_status, 200)
        self.assertEqual(sniff_ext(thumb_bytes), 'jpg')

    def login_token(self) -> str:
        """Log in as owner and return the bearer token."""
        status, data = self.api_call('/api/login', 'POST', json.dumps({'password': 'debugpassword'}).encode(),
                                     {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        return data['token']

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

    def test_auth_and_rescan_guard(self) -> None:
        """Unauthenticated calls to owner routes (upload, rescan, thumbnails) return 401 cleanly."""
        self.assertEqual(self.api_call('/api/photos', 'POST', TINY_JPEG)[0], 401)
        self.assertEqual(self.api_call('/api/rescan', 'POST', b'')[0], 401)
        self.assertEqual(self.api_call('/api/thumbnails', 'GET')[0], 401)
        self.assertEqual(self.api_call('/api/thumbnails', 'POST', b'')[0], 401)

        # Authenticated rescan adopts a hand-copied file.
        (PHOTO_DIR / 'Copied_Photo.jpg').write_bytes(TINY_JPEG)
        token = self.login_token()
        status, scan_reply = self.api_call('/api/rescan', 'POST', b'', {'Authorization': f'Bearer {token}'})
        self.assertEqual(status, 200)
        self.assertEqual(scan_reply['added'], 1)


if __name__ == '__main__':
    unittest.main()
