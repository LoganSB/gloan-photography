"""Owner authentication: WebAuthn Passkeys (multi-device), Cloudflare Turnstile CAPTCHA and signed session tokens.

Passkeys (FIDO2 / WebAuthn) are the sole Admin login mechanism (zero password routes or debug backdoors):
- Browser calls `navigator.credentials.create({publicKey: {pubKeyCredParams: [{alg: -7, type: 'public-key'}]}})`
  using the `getPublicKey()` / `getPublicKeyAlgorithm()` extensions so the server receives the raw SPKI public key
  without external CBOR dependencies.
- The server verifies the challenge, origin, rpIdHash, UP/UV flags, signature counter and ECDSA P-256 (ES256)
  signature using `cryptography`, storing only the public keys and credential ids in `data/passkey.json`.
- Supports multiple registered passkeys (`{"credentials": [...]}`) so the owner can enroll their phone, desktop,
  laptop and backup security keys, while remaining backwards-compatible with a single-passkey `passkey.json`.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import struct
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key

from gloan.config import PASSKEY_FILE, SESSION_KEY_FILE, TOKEN_TTL

FAILS: dict[str, tuple[int, float]] = {}  # ip -> (failed attempts, locked until)
FAILS_LOCK = threading.Lock()
MAX_TRACKED_IPS = 4096
MAX_ACTIVE_CHALLENGES = 512
MAX_PASSKEYS = 16
CHALLENGE_TTL_SECONDS = 300
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'
RP_ID_RE = re.compile(r'^[a-z0-9.-]{1,253}$')

_passkey_lock = threading.Lock()
_challenges: dict[str, tuple[str, float]] = {}  # challenge_b64 -> (purpose 'register'|'login', expires_at)
_passkey_generation = 0
_cached_passkey_stamp: tuple[int, int, int] | None = None
_cached_passkeys: list[dict[str, Any]] = []


def b64url_encode(raw: bytes) -> str:
    """Encode bytes as unpadded base64url (standard WebAuthn binary representation in JSON)."""
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def b64url_decode(encoded: str) -> bytes:
    """Decode an unpadded or padded base64url string into bytes; raise ValueError on malformed input."""
    if not isinstance(encoded, str) or not re.fullmatch(r'[A-Za-z0-9_-]+=*', encoded.strip()):
        raise ValueError('invalid base64url encoding')
    cleaned = encoded.strip().rstrip('=')
    if len(cleaned) % 4 == 1:
        raise ValueError('invalid base64url length')
    padded = cleaned + '=' * (-len(cleaned) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode('ascii'))
    except Exception as exc:
        raise ValueError('invalid base64url encoding') from exc


def normalize_ip(raw: str) -> str:
    """Canonical text for an IP so '::ffff:1.2.3.4' and '1.2.3.4' compare equal; junk is returned unchanged."""
    try:
        addr = ipaddress.ip_address(raw.strip())
    except ValueError:
        return raw.strip()
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


# -- Cloudflare Turnstile Bot CAPTCHA ----------------------------------------

def turnstile_site_key() -> str:
    """Configured Cloudflare Turnstile public site key, or '' when CAPTCHA is not enabled."""
    return os.environ.get('GLOAN_TURNSTILE_SITE_KEY', '').strip()


def turnstile_secret_key() -> str:
    """Configured Cloudflare Turnstile secret key, or '' when CAPTCHA is not enabled."""
    return os.environ.get('GLOAN_TURNSTILE_SECRET_KEY', '').strip()


def turnstile_enabled() -> bool:
    """True when both Cloudflare Turnstile keys are configured in the environment."""
    return bool(turnstile_site_key() and turnstile_secret_key())


def verify_turnstile(token: str, remote_ip: str = '') -> bool:
    """Verify a Cloudflare Turnstile response token against Cloudflare's siteverify endpoint.

    When Turnstile is not configured (both env vars empty), returns True so local development works out of the box.
    """
    secret = turnstile_secret_key()
    if not secret or not turnstile_site_key():
        return True
    if not token or not isinstance(token, str):
        return False
    payload = json.dumps({'secret': secret, 'response': token, 'remoteip': remote_ip}).encode('utf-8')
    req = Request(TURNSTILE_VERIFY_URL, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urlopen(req, timeout=5) as response:
            body = json.loads(response.read())
            return bool(body.get('success'))
    except (URLError, ValueError, OSError):
        return False


# -- WebAuthn / Passkey Storage & Verification (Multi-Device) ----------------

def _normalize_passkey_records(raw_data: Any) -> list[dict[str, Any]]:
    """Accept either legacy single-passkey dict `{"credentialId": ...}` or multi-device `{"credentials": [...]}`."""
    if isinstance(raw_data, dict):
        if isinstance(raw_data.get('credentials'), list):
            items = raw_data['credentials']
        elif raw_data.get('credentialId') and raw_data.get('publicKeySpki'):
            items = [raw_data]
        else:
            items = []
    elif isinstance(raw_data, list):
        items = raw_data
    else:
        items = []
    return [
        dict(item) for item in items
        if isinstance(item, dict) and item.get('credentialId') and item.get('publicKeySpki')
    ]


def _passkey_file_stamp() -> tuple[int, int]:
    """Return (mtime_ns, size) of passkey.json, or (0, 0) when missing."""
    try:
        info = PASSKEY_FILE.stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return 0, 0


def load_passkeys() -> list[dict[str, Any]]:
    """Read all registered owner passkeys (`[{credentialId, publicKeySpki, signCount, created}, ...]`).

    Caches the parsed credential list in memory keyed on `(generation, mtime_ns, size)` so frequent
    `GET /api/session` requests do not repeatedly read and parse `passkey.json` over Cloud Storage FUSE.
    Backwards-compatible with a single-credential `data/passkey.json` created on earlier deployments, and also
    honours `GLOAN_OWNER_PASSKEY` if pinned via environment variable.
    """
    global _cached_passkey_stamp, _cached_passkeys  # pylint: disable=global-statement
    env_json = os.environ.get('GLOAN_OWNER_PASSKEY', '').strip()
    if env_json:
        try:
            records = _normalize_passkey_records(json.loads(env_json))
            if records:
                return records
        except ValueError:
            pass
    with _passkey_lock:
        stamp = (_passkey_generation, *_passkey_file_stamp())
        if _cached_passkey_stamp != stamp:
            if stamp[1:] == (0, 0):
                _cached_passkeys = []
            else:
                try:
                    _cached_passkeys = _normalize_passkey_records(json.loads(PASSKEY_FILE.read_text(encoding='utf-8')))
                except (FileNotFoundError, ValueError, OSError):
                    _cached_passkeys = []
            _cached_passkey_stamp = stamp
        return [dict(item) for item in _cached_passkeys]


def save_passkeys(records: list[dict[str, Any]]) -> None:
    """Atomically persist the list of registered owner passkeys to `data/passkey.json` and update the cache."""
    global _passkey_generation, _cached_passkey_stamp, _cached_passkeys  # pylint: disable=global-statement
    bounded = [dict(item) for item in records[:MAX_PASSKEYS]]
    with _passkey_lock:
        PASSKEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PASSKEY_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'credentials': bounded}, indent=2), encoding='utf-8')
        tmp.replace(PASSKEY_FILE)
        _passkey_generation += 1
        _cached_passkeys = bounded
        _cached_passkey_stamp = (_passkey_generation, *_passkey_file_stamp())


def validate_rp_id(raw_host: str) -> str:
    """Extract and validate the WebAuthn Relying Party ID (hostname without port) from the HTTP Host header."""
    rp_id = (raw_host or 'localhost').split(':', 1)[0].strip().lower()
    if not rp_id or not RP_ID_RE.fullmatch(rp_id) or '..' in rp_id:
        raise ValueError('invalid host header for WebAuthn rpId')
    return rp_id


def has_passkey() -> bool:
    """True when at least one owner passkey has been registered."""
    return len(load_passkeys()) > 0


def issue_webauthn_challenge(purpose: str) -> str:
    """Generate and store a one-time 32-byte WebAuthn challenge for `purpose` ('register' or 'login').

    Caps the active challenge map at MAX_ACTIVE_CHALLENGES (evicting oldest entries first) so an attacker
    flooding `/api/passkey/*/options` can never exhaust server memory.
    """
    now = time.time()
    challenge = b64url_encode(secrets.token_bytes(32))
    with _passkey_lock:
        expired = [key for key, (_, exp) in _challenges.items() if exp <= now]
        for key in expired:
            _challenges.pop(key, None)
        while len(_challenges) >= MAX_ACTIVE_CHALLENGES:
            oldest_key = next(iter(_challenges))
            _challenges.pop(oldest_key, None)
        _challenges[challenge] = (purpose, now + CHALLENGE_TTL_SECONDS)
    return challenge


def consume_webauthn_challenge(challenge: str, expected_purpose: str) -> bool:
    """Consume a one-time challenge if it exists, matches `expected_purpose`, and has not expired."""
    with _passkey_lock:
        entry = _challenges.pop(challenge, None)
    if not entry:
        return False
    purpose, expires_at = entry
    return purpose == expected_purpose and expires_at > time.time()


def _verify_client_data_and_auth_data(
    client_data_b64: str,
    auth_data_b64: str,
    expected_type: str,
    expected_purpose: str,
    expected_host: str,
) -> tuple[bytes, bytes, int]:
    """Validate WebAuthn clientDataJSON and authenticatorData, returning (client_data_bytes, auth_data_bytes, sign_count).

    Checks:
    1. clientDataJSON.type == expected_type ('webauthn.create' or 'webauthn.get')
    2. clientDataJSON.crossOrigin is not True
    3. clientDataJSON.challenge is a valid, unconsumed challenge issued for expected_purpose
    4. clientDataJSON.origin hostname matches expected_host and uses https (or localhost for local dev)
    5. authenticatorData rpIdHash == SHA-256(rp_id)
    6. authenticatorData User Present (bit 0) is set
    """
    if len(client_data_b64) > 4096 or len(auth_data_b64) > 1024:
        raise ValueError('WebAuthn field exceeds maximum size')
    client_data_bytes = b64url_decode(client_data_b64)
    auth_data_bytes = b64url_decode(auth_data_b64)
    if len(auth_data_bytes) < 37:
        raise ValueError('authenticatorData too short')

    try:
        client_data = json.loads(client_data_bytes.decode('utf-8'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError('malformed clientDataJSON') from exc
    if not isinstance(client_data, dict):
        raise ValueError('clientDataJSON must be an object')
    if client_data.get('type') != expected_type:
        raise ValueError('unexpected WebAuthn ceremony type')
    if client_data.get('crossOrigin') is True:
        raise ValueError('cross-origin WebAuthn ceremonies are not allowed')
    challenge = str(client_data.get('challenge', ''))
    if not consume_webauthn_challenge(challenge, expected_purpose):
        raise ValueError('invalid or expired WebAuthn challenge')

    origin = str(client_data.get('origin', ''))
    parsed_origin = urlparse(origin)
    origin_scheme = (parsed_origin.scheme or '').lower()
    origin_host = (parsed_origin.hostname or '').lower()
    rp_id = validate_rp_id(expected_host)
    if not origin_host or origin_host != rp_id:
        raise ValueError(f'origin {origin_host!r} does not match host {rp_id!r}')
    if origin_scheme != 'https' and origin_host not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('WebAuthn origin must use https')

    expected_rp_hash = hashlib.sha256(rp_id.encode('utf-8')).digest()
    if not hmac.compare_digest(auth_data_bytes[:32], expected_rp_hash):
        raise ValueError('rpIdHash mismatch')

    flags = auth_data_bytes[32]
    if not (flags & 0x01):  # UP (User Present) bit
        raise ValueError('user presence flag not set')

    sign_count = struct.unpack('>I', auth_data_bytes[33:37])[0]
    return client_data_bytes, auth_data_bytes, sign_count


def register_passkey_credential(payload: dict[str, Any], expected_host: str) -> dict[str, Any]:
    """Validate a WebAuthn registration payload and append the new passkey to `data/passkey.json`."""
    credential_id = str(payload.get('credentialId', '')).strip()
    public_key_spki_b64 = str(payload.get('publicKey', '')).strip()
    client_data_b64 = str(payload.get('clientDataJSON', '')).strip()
    auth_data_b64 = str(payload.get('authenticatorData', '')).strip()
    if not (credential_id and public_key_spki_b64 and client_data_b64 and auth_data_b64):
        raise ValueError('missing passkey registration fields')
    if len(credential_id) > 1400 or len(public_key_spki_b64) > 1024:
        raise ValueError('passkey registration field exceeds maximum size')

    # Verify credentialId is valid base64url and publicKey is a valid EC P-256 SPKI DER key.
    raw_cred_id = b64url_decode(credential_id)
    if not 1 <= len(raw_cred_id) <= 1024:
        raise ValueError('invalid credentialId length')
    spki_bytes = b64url_decode(public_key_spki_b64)
    try:
        pub_key = load_der_public_key(spki_bytes)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise ValueError('invalid SPKI public key') from exc
    if not isinstance(pub_key, ec.EllipticCurvePublicKey) or not isinstance(pub_key.curve, ec.SECP256R1):
        raise ValueError('passkey must use an ES256 (P-256) public key')
    canonical_spki_b64 = b64url_encode(pub_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo))

    _, _, sign_count = _verify_client_data_and_auth_data(
        client_data_b64, auth_data_b64, 'webauthn.create', 'register', expected_host
    )
    existing = load_passkeys()
    if len(existing) >= MAX_PASSKEYS and not any(item['credentialId'] == credential_id for item in existing):
        raise ValueError(f'maximum of {MAX_PASSKEYS} passkeys reached')
    record = {
        'credentialId': credential_id,
        'publicKeySpki': canonical_spki_b64,
        'signCount': sign_count,
        'created': int(time.time() * 1000),
    }
    updated = [item for item in existing if item['credentialId'] != credential_id]
    updated.append(record)
    save_passkeys(updated)
    return record


def verify_passkey_assertion(payload: dict[str, Any], expected_host: str) -> bool:
    """Verify a WebAuthn assertion (login) signature against any of the owner's registered passkeys."""
    records = load_passkeys()
    if not records:
        raise ValueError('no passkey registered')

    credential_id = str(payload.get('credentialId', '')).strip()
    matched_record = next(
        (item for item in records if hmac.compare_digest(credential_id, str(item.get('credentialId', '')))),
        None,
    )
    if not matched_record:
        raise ValueError('unknown passkey credential')

    client_data_bytes, auth_data_bytes, sign_count = _verify_client_data_and_auth_data(
        str(payload.get('clientDataJSON', '')),
        str(payload.get('authenticatorData', '')),
        'webauthn.get',
        'login',
        expected_host,
    )
    raw_signature = str(payload.get('signature', '')).strip()
    if len(raw_signature) > 256:
        raise ValueError('passkey signature exceeds maximum size')
    signature_bytes = b64url_decode(raw_signature)
    pub_key = load_der_public_key(b64url_decode(matched_record['publicKeySpki']))
    if not isinstance(pub_key, ec.EllipticCurvePublicKey):
        raise ValueError('stored key is not EC')

    signed_data = auth_data_bytes + hashlib.sha256(client_data_bytes).digest()
    try:
        pub_key.verify(signature_bytes, signed_data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError('invalid passkey signature') from exc

    # Verify and advance signature counter when the authenticator supports one (non-zero).
    previous_count = int(matched_record.get('signCount', 0))
    if previous_count > 0 and sign_count <= previous_count:
        raise ValueError('cloned passkey detected (signCount did not advance)')
    if sign_count > previous_count:
        matched_record['signCount'] = sign_count
        save_passkeys(records)
    return True


def _load_or_create_session_secret() -> bytes:
    """Load or create a persistent 32-byte secret key in `data/session.key` for signing session tokens."""
    try:
        raw = SESSION_KEY_FILE.read_bytes()
        if len(raw) >= 32:
            return raw
    except FileNotFoundError:
        pass
    secret = secrets.token_bytes(32)
    try:
        SESSION_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_KEY_FILE.write_bytes(secret)
        os.chmod(SESSION_KEY_FILE, 0o600)
    except OSError:
        pass
    return secret


class Auth:
    """Stateless owner sessions: an expiry timestamp signed with a persistent HMAC-SHA256 key (`data/session.key`)."""

    def __init__(self, secret_seed: bytes | None = None) -> None:
        seed = secret_seed if secret_seed else _load_or_create_session_secret()
        self.key = hashlib.sha256(b'gloan-session:' + seed).digest()

    def _sig(self, payload: bytes) -> str:
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def issue(self) -> str:
        """Mint a token valid for TOKEN_TTL seconds."""
        payload = str(int(time.time()) + TOKEN_TTL).encode('ascii')
        return base64.urlsafe_b64encode(payload).decode('ascii') + '.' + self._sig(payload)

    def verify(self, token: str) -> bool:
        """True when the token's signature matches and it has not expired."""
        if not isinstance(token, str) or len(token) > 256 or '.' not in token:
            return False
        try:
            b64, sig = token.split('.', 1)
            payload = base64.urlsafe_b64decode(b64.encode('ascii'))
            ok = hmac.compare_digest(sig, self._sig(payload)) and int(payload) > time.time()
        except (ValueError, TypeError):
            ok = False
        return ok
