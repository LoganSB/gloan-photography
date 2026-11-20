"""Owner sessions (signed tokens), client-address normalisation and login-lockout state."""

import base64
import hashlib
import hmac
import ipaddress
import time

from gloan.config import TOKEN_TTL


FAILS: dict[str, tuple[int, float]] = {}  # ip -> (failed attempts, locked until)


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
