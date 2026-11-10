"""Two-factor authentication with authenticator apps (TOTP, RFC 6238) and recovery codes.

An authenticator app (Microsoft or Google Authenticator, 1Password, Authy, ...) and the
server share a random secret. Every 30 seconds both compute
    HMAC-SHA1(secret, number of 30-second steps since 1970) -> 6 digits.
A code proves the user holds the device with the secret. Standard library only.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

STEP = 30      # seconds per code
DIGITS = 6
WINDOW = 1     # accept one step either side, for clock drift
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i


def new_secret() -> str:
    """160 random bits, base32-encoded (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    secret = secret.strip().replace(" ", "").upper()
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def code_at(secret: str, step: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP for one time step (dynamic truncation of HMAC-SHA1)."""
    mac = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP)


def verify(secret: str, code: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """The time step the code belongs to, or None. A step at or before last_step is
    refused, so a code can't be used twice (replay protection)."""
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != DIGITS:
        return None
    step = current_step(now)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if last_step is not None and candidate <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, candidate), code):
            return candidate
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "Invoice Review") -> str:
    """The otpauth:// link an authenticator app reads from the QR code."""
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                       "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{query}"


# ---------------------------------------------------------------- recovery codes
def new_recovery_codes(count: int = 10) -> list[str]:
    """One-time codes for when the phone is lost, e.g. 'k7mq-x2rv-p9tc'. ~74 bits each."""
    def one() -> str:
        raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(15))
        return "-".join(raw[i:i + 5] for i in range(0, 15, 5))
    return [one() for _ in range(count)]


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch in RECOVERY_ALPHABET)


def hash_recovery_code(code: str, pepper: bytes) -> str:
    """Recovery codes are long random strings, so a keyed fast hash is enough (unlike
    passwords, they can't be guessed from a dictionary). The pepper is a server secret."""
    return hmac.new(pepper, normalize_recovery_code(code).encode(), hashlib.sha256).hexdigest()


def use_recovery_code(code: str, hashes: list[str], pepper: bytes) -> list[str] | None:
    """If the code matches one of the stored hashes, return the remaining hashes (the code
    is used up); otherwise None. Every hash is compared, in constant time."""
    candidate = hash_recovery_code(code, pepper)
    match = None
    for h in hashes:
        if hmac.compare_digest(h, candidate):
            match = h
    if match is None:
        return None
    return [h for h in hashes if h != match]
