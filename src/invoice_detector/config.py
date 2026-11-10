"""Settings come from environment variables (optionally via a local .env file).

Secrets never live in code. Secret fields are hidden from repr() so they can't leak
into logs by accident.
"""
import base64
import os
from dataclasses import dataclass, field


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env reader. Real environment variables win over the file.
    Also accepts UTF-16 files, which Windows PowerShell's `>` redirect produces."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        raw = f.read()
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    for line in raw.decode(encoding).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_keys(raw: str) -> dict[int, bytes]:
    """'1:<base64>,2:<base64>' -> {1: key1, 2: key2}. Newest key = highest id."""
    keys: dict[int, bytes] = {}
    for part in raw.split(","):
        kid, _, b64 = part.strip().partition(":")
        key = base64.b64decode(b64)
        if len(key) != 32:
            raise RuntimeError("Each encryption key must be 32 bytes (base64-encoded)")
        keys[int(kid)] = key
    return keys


@dataclass(frozen=True)
class Settings:
    secret_key: str = field(repr=False)
    encryption_keys: dict = field(repr=False)
    index_key: bytes = field(repr=False)
    db_path: str = "invoices.db"
    anthropic_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        secret = _require("SECRET_KEY")
        if len(secret) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters")
        index_key = base64.b64decode(_require("INDEX_KEY"))
        if len(index_key) != 32:
            raise RuntimeError("INDEX_KEY must be 32 bytes (base64-encoded)")
        return cls(
            secret_key=secret,
            encryption_keys=parse_keys(_require("ENCRYPTION_KEYS")),
            index_key=index_key,
            db_path=os.environ.get("DB_PATH", "invoices.db"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        )
