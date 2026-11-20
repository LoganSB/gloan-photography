"""Owner authentication: WebAuthn Passkeys, Cloudflare Turnstile CAPTCHA, signed session tokens and debug password.

Passkeys (FIDO2 / WebAuthn) are the primary Admin login mechanism:
- Browser calls `navigator.credentials.create({publicKey: {pubKeyCredParams: [{alg: -7, type: 'public-key'}]}})`
  using the `getPublicKey()` / `getPublicKeyAlgorithm()` extensions so the server receives the raw SPKI public key
  without external CBOR dependencies.
- The server verifies the challenge, origin, rpIdHash, UP/UV flags, signature counter and ECDSA P-256 (ES256)
  signature using `cryptography`, storing only the public key and credential id in `data/passkey.json`.

The debug password workaround (`GLOAN_PASSWORD` / `DEBUG_PASSWORD_AUTH`) is isolated in a single clearly-marked
section below so it can be deleted in one step once the Passkey flow is verified in the cloud.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import struct
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_der_public_key

from gloan.config import PASSKEY_FILE, SESSION_KEY_FILE, TOKEN_TTL

FAILS: dict[str, tuple[int, float]] = {}  # ip -> (failed attempts, locked until)
CHALLENGE_TTL_SECONDS = 300
TURNSTILE_VERIFY_URL = 'https://challenges.cloudflare.com/turnstile/v0/siteverify'

_passkey_lock = threading.Lock()
_challenges: dict[str, tuple[str, float]] = {}  # challenge_b64 -> (purpose 'register'|'login', expires_at)


def b64url_encode(raw: bytes) -> str:
    """Encode bytes as unpadded base64url (standard WebAuthn binary representation in JSON)."""
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def b64url_decode(encoded: str) -> bytes:
    """Decode an unpadded or padded base64url string into bytes."""
    padded = encoded.strip() + '=' * (-len(encoded.strip()) % 4)
    return base64.urlsafe_b64decode(padded.encode('ascii'))


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


# -- WebAuthn / Passkey Storage & Verification -------------------------------

def load_passkey() -> dict[str, Any] | None:
    """Read the registered owner passkey (`{credentialId, publicKeySpki, signCount}`) from disk or env.

    Also honours `GLOAN_OWNER_PASSKEY` (a JSON string) so the passkey can optionally be pinned in Secret Manager.
    """
    env_json = os.environ.get('GLOAN_OWNER_PASSKEY', '').strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict) and data.get('credentialId') and data.get('publicKeySpki'):
                return data
        except ValueError:
            pass
    with _passkey_lock:
        try:
            data = json.loads(PASSKEY_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data.get('credentialId') and data.get('publicKeySpki'):
                return data
        except (FileNotFoundError, ValueError):
            pass
    return None


def save_passkey(record: dict[str, Any]) -> None:
    """Atomically persist the owner passkey record to `data/passkey.json`."""
    with _passkey_lock:
        PASSKEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PASSKEY_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(record, indent=2), encoding='utf-8')
        tmp.replace(PASSKEY_FILE)


def has_passkey() -> bool:
    """True when an owner passkey has been registered."""
    return load_passkey() is not None


def issue_webauthn_challenge(purpose: str) -> str:
    """Generate and store a one-time 32-byte WebAuthn challenge for `purpose` ('register' or 'login')."""
    now = time.time()
    challenge = b64url_encode(secrets.token_bytes(32))
    with _passkey_lock:
        expired = [key for key, (_, exp) in _challenges.items() if exp <= now]
        for key in expired:
            _challenges.pop(key, None)
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
    2. clientDataJSON.challenge is a valid, unconsumed challenge issued for expected_purpose
    3. clientDataJSON.origin hostname matches expected_host (the HTTP Host / RP ID)
    4. authenticatorData rpIdHash == SHA-256(rp_id)
    5. authenticatorData User Present (bit 0) is set
    """
    client_data_bytes = b64url_decode(client_data_b64)
    auth_data_bytes = b64url_decode(auth_data_b64)
    if len(auth_data_bytes) < 37:
        raise ValueError('authenticatorData too short')

    client_data = json.loads(client_data_bytes.decode('utf-8'))
    if client_data.get('type') != expected_type:
        raise ValueError('unexpected WebAuthn ceremony type')
    challenge = str(client_data.get('challenge', ''))
    if not consume_webauthn_challenge(challenge, expected_purpose):
        raise ValueError('invalid or expired WebAuthn challenge')

    origin = str(client_data.get('origin', ''))
    origin_host = (urlparse(origin).hostname or '').lower()
    rp_id = expected_host.split(':', 1)[0].strip().lower()
    if not origin_host or origin_host != rp_id:
        raise ValueError(f'origin {origin_host!r} does not match host {rp_id!r}')

    expected_rp_hash = hashlib.sha256(rp_id.encode('utf-8')).digest()
    if not hmac.compare_digest(auth_data_bytes[:32], expected_rp_hash):
        raise ValueError('rpIdHash mismatch')

    flags = auth_data_bytes[32]
    if not (flags & 0x01):  # UP (User Present) bit
        raise ValueError('user presence flag not set')

    sign_count = struct.unpack('>I', auth_data_bytes[33:37])[0]
    return client_data_bytes, auth_data_bytes, sign_count


def register_passkey_credential(payload: dict[str, Any], expected_host: str) -> dict[str, Any]:
    """Validate a WebAuthn registration payload and store the new owner passkey in `data/passkey.json`."""
    credential_id = str(payload.get('credentialId', '')).strip()
    public_key_spki_b64 = str(payload.get('publicKey', '')).strip()
    client_data_b64 = str(payload.get('clientDataJSON', '')).strip()
    auth_data_b64 = str(payload.get('authenticatorData', '')).strip()
    if not (credential_id and public_key_spki_b64 and client_data_b64 and auth_data_b64):
        raise ValueError('missing passkey registration fields')

    # Verify credentialId is valid base64url and publicKey is a valid EC P-256 SPKI DER key.
    b64url_decode(credential_id)
    spki_bytes = b64url_decode(public_key_spki_b64)
    pub_key = load_der_public_key(spki_bytes)
    if not isinstance(pub_key, ec.EllipticCurvePublicKey) or not isinstance(pub_key.curve, ec.SECP256R1):
        raise ValueError('passkey must use an ES256 (P-256) public key')
    canonical_spki_b64 = b64url_encode(pub_key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo))

    _, _, sign_count = _verify_client_data_and_auth_data(
        client_data_b64, auth_data_b64, 'webauthn.create', 'register', expected_host
    )
    record = {
        'credentialId': credential_id,
        'publicKeySpki': canonical_spki_b64,
        'signCount': sign_count,
        'created': int(time.time() * 1000),
    }
    save_passkey(record)
    return record


def verify_passkey_assertion(payload: dict[str, Any], expected_host: str) -> bool:
    """Verify a WebAuthn assertion (login) signature against the stored owner passkey."""
    stored = load_passkey()
    if not stored:
        raise ValueError('no passkey registered')

    credential_id = str(payload.get('credentialId', '')).strip()
    if not hmac.compare_digest(credential_id, str(stored.get('credentialId', ''))):
        raise ValueError('unknown passkey credential')

    client_data_bytes, auth_data_bytes, sign_count = _verify_client_data_and_auth_data(
        str(payload.get('clientDataJSON', '')),
        str(payload.get('authenticatorData', '')),
        'webauthn.get',
        'login',
        expected_host,
    )
    signature_bytes = b64url_decode(str(payload.get('signature', '')))
    pub_key = load_der_public_key(b64url_decode(stored['publicKeySpki']))
    if not isinstance(pub_key, ec.EllipticCurvePublicKey):
        raise ValueError('stored key is not EC')

    signed_data = auth_data_bytes + hashlib.sha256(client_data_bytes).digest()
    try:
        pub_key.verify(signature_bytes, signed_data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise ValueError('invalid passkey signature') from exc

    # Verify and advance signature counter when the authenticator supports one (non-zero).
    previous_count = int(stored.get('signCount', 0))
    if previous_count > 0 and sign_count <= previous_count:
        raise ValueError('cloned passkey detected (signCount did not advance)')
    if sign_count > previous_count:
        stored['signCount'] = sign_count
        save_passkey(stored)
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
    except OSError:
        pass
    return secret


class Auth:
    """Stateless owner sessions: an expiry timestamp signed with an HMAC-SHA256 key.

    # ========================================================================
    # DEBUG PASSWORD WORKAROUND (REMOVE AFTER CLOUD PASSKEY VERIFICATION)
    # When `debug_password` is non-empty (set via GLOAN_PASSWORD), password
    # login is enabled alongside Passkey login so local Docker debugging and
    # initial cloud passkey enrollment work easily. To disable password auth
    # completely, unset GLOAN_PASSWORD or set `debug_password = ''`.
    # ========================================================================
    """

    def __init__(self, debug_password: str = '') -> None:
        self.debug_password = debug_password.encode('utf-8')
        seed = self.debug_password if self.debug_password else _load_or_create_session_secret()
        self.key = hashlib.sha256(b'gloan-session:' + seed).digest()

    @property
    def debug_password_enabled(self) -> bool:
        """True only while a debug password (`GLOAN_PASSWORD`) is configured."""
        return bool(self.debug_password)

    def check_password(self, given: str) -> bool:
        """Constant-time check against the debug password; always False when debug password is disabled."""
        if not self.debug_password or not given:
            return False
        return hmac.compare_digest(
            hashlib.sha256(given.encode('utf-8')).digest(),
            hashlib.sha256(self.debug_password).digest(),
        )

    def _sig(self, payload: bytes) -> str:
        return hmac.new(self.key, payload, hashlib.sha256).hexdigest()

    def issue(self) -> str:
        """Mint a token valid for TOKEN_TTL seconds."""
        payload = str(int(time.time()) + TOKEN_TTL).encode('ascii')
        return base64.urlsafe_b64encode(payload).decode('ascii') + '.' + self._sig(payload)

    def verify(self, token: str) -> bool:
        """True when the token's signature matches and it has not expired."""
        try:
            b64, sig = token.split('.', 1)
            payload = base64.urlsafe_b64decode(b64.encode('ascii'))
            ok = hmac.compare_digest(sig, self._sig(payload)) and int(payload) > time.time()
        except ValueError:
            ok = False
        return ok
