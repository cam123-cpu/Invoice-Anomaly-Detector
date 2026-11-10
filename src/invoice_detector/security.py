"""Password hashing, signed tokens and login rate limiting (standard library only)."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

N, R, P = 2**14, 8, 1  # scrypt cost; stored inside each hash so it can be raised later


class AuthError(Exception):
    pass


class RateLimitError(Exception):
    pass


# ---------- passwords ----------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p))
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# Verified against when a username doesn't exist, so timing doesn't reveal valid users.
DUMMY_HASH = hash_password("not-a-real-password")


# ---------- rate limiting ----------
class RateLimiter:
    """Sliding window of failures per key, then a lockout."""

    def __init__(self, max_attempts: int = 5, window: int = 900, lockout: int = 900):
        self.max, self.window, self.lockout = max_attempts, window, lockout
        self._failures: dict[str, deque] = defaultdict(deque)
        self._locked_until: dict[str, float] = {}

    def check(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if self._locked_until.get(key, 0) > now:
            raise RateLimitError("Too many attempts")

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        q = self._failures[key]
        q.append(now)
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.max:
            self._locked_until[key] = now + self.lockout
            q.clear()

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)


# ---------- signed, expiring tokens ----------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class TokenSigner:
    def __init__(self, secret: str):
        self._key = secret.encode()

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def make(self, user: str, role: str, ttl: int = 3600, now: float | None = None) -> str:
        now = time.time() if now is None else now
        body = _b64(json.dumps({"sub": user, "role": role, "exp": now + ttl}).encode())
        return f"{body}.{self._sign(body)}"

    def read(self, token: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        try:
            body, sig = token.split(".")
            if not hmac.compare_digest(sig, self._sign(body)):
                raise AuthError("Bad token")
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if data["exp"] < now:
                raise AuthError("Token expired")
            return {"sub": data["sub"], "role": data["role"]}
        except (ValueError, KeyError) as exc:
            raise AuthError("Malformed token") from exc
