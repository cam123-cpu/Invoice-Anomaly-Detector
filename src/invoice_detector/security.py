"""Password hashing, signed tokens and login rate limiting (standard library only)."""
import base64
import hashlib
import hmac
import json
import re
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

    MAX_KEYS = 50_000  # a bot trying many usernames can't grow memory without bound

    def _prune(self, now: float) -> None:
        self._locked_until = {k: t for k, t in self._locked_until.items() if t > now}
        for k in [k for k, q in self._failures.items() if not q or q[-1] < now - self.window]:
            del self._failures[k]
        if len(self._failures) > self.MAX_KEYS:  # still too many: forget the oldest
            for k in sorted(self._failures, key=lambda k: self._failures[k][-1])[:len(self._failures) // 2]:
                del self._failures[k]

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if len(self._failures) > self.MAX_KEYS or len(self._locked_until) > self.MAX_KEYS:
            self._prune(now)
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


def password_fingerprint(password_hash: str) -> str:
    """A short, non-reversible tag of the stored password hash. Tokens carry it, so changing
    the password ends every session and makes old reset links useless."""
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


# Long enough to pass a length rule, but among the first things an attacker tries.
_COMMON_WORDS = {"password", "passw0rd", "p@ssword", "p@ssw0rd", "qwerty", "qwertyuiop", "letmein", "welcome",
                 "admin", "administrator", "changeme", "iloveyou", "monkey", "dragon", "football", "baseball",
                 "sunshine", "princess", "trustno", "master", "login", "invoice", "invoices", "accounting",
                 "finance", "company", "secret", "abc", "test", "temp", "default"}
_RUNS = ("01234567890123456789", "abcdefghijklmnopqrstuvwxyz", "qwertyuiopasdfghjklzxcvbnm",
         "98765432109876543210")


def password_problem(password: str, username: str = "") -> str | None:
    """Why a password is too easy to guess, or None. Length alone (12+) is checked elsewhere;
    this refuses the long-but-obvious ones: common words with digits on the end, runs,
    repeats, and the username itself."""
    low = password.lower()
    core = re.sub(r"[\d\W_]+$", "", re.sub(r"^[\d\W_]+", "", low))   # "Password1234!" -> "password"
    if len(set(low)) < 5:
        return "Choose a password with more variety: this one repeats a few characters."
    if core in _COMMON_WORDS or low in _COMMON_WORDS:
        return "That password is one of the first an attacker would try. Choose something less common."
    if any(low in run for run in _RUNS):
        return "Choose a password that isn't a run of keys like 123456 or qwerty."
    name = username.lower().split("@")[0]
    if len(name) >= 3 and name in low:
        rest = low.replace(name, "")
        if len(rest) < 8 or len(re.sub(r"[^a-z]", "", rest)) < 4:   # the name plus digits or symbols
            return "Choose a password that isn't based on the username."
    return None


def session_fingerprint(user: dict) -> str:
    """What every token is tied to: the password hash and the sign-out counter. Change the
    password, sign out, or have access removed, and every earlier token stops working."""
    return password_fingerprint(f"{user['password_hash']}|{user.get('session_epoch') or 0}")


class TokenSigner:
    """Signed, expiring tokens, each with a purpose. An access token, a "second factor
    pending" token and a password-reset link are all signed with the same key, so the
    purpose is checked on every read: a reset link can never be used to sign in."""

    def __init__(self, secret: str):
        self._key = secret.encode()

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def issue(self, claims: dict, purpose: str, ttl: int, now: float | None = None) -> str:
        now = time.time() if now is None else now
        body = _b64(json.dumps({**claims, "purpose": purpose, "exp": now + ttl}).encode())
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, purpose: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        try:
            body, sig = token.split(".")
            if not hmac.compare_digest(sig, self._sign(body)):
                raise AuthError("Bad token")
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if data["exp"] < now:
                raise AuthError("Token expired")
            if data.get("purpose", "access") != purpose:
                raise AuthError("Wrong kind of token")
            return data
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise AuthError("Malformed token") from exc

    # ----- access tokens (sent with every API request) -----
    def make(self, user: str, role: str, org: str, ttl: int = 3600, now: float | None = None,
             mfa: bool = False, pwf: str | None = None) -> str:
        return self.issue({"sub": user, "role": role, "org": org, "mfa": mfa, "pwf": pwf},
                          "access", ttl, now)

    def read(self, token: str, now: float | None = None) -> dict:
        data = self.verify(token, "access", now)
        try:
            return {"sub": data["sub"], "role": data["role"], "org": data["org"],
                    "mfa": bool(data.get("mfa")), "pwf": data.get("pwf")}
        except KeyError as exc:
            raise AuthError("Malformed token") from exc
