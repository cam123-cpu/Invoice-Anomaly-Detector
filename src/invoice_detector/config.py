"""Settings come from environment variables (optionally via a local .env file).

Secrets never live in code. In production they come from a secrets manager, which hands
them to the app in one of two standard ways, both supported here:
- as environment variables (AWS ECS, Google Cloud Run, Azure, Render, Fly, Heroku), or
- as files, named by NAME_FILE (Docker and Kubernetes secrets): SECRET_KEY_FILE=/run/secrets/secret_key.

Secret fields are hidden from repr() so they can't leak into logs by accident.
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


def env(name: str, default: str | None = None) -> str | None:
    """NAME from the environment, or the contents of the file named by NAME_FILE."""
    value = os.environ.get(name)
    if value:
        return value
    path = os.environ.get(name + "_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or default
        except OSError as exc:
            raise RuntimeError(f"{name}_FILE points to {path}, which can't be read") from exc
    return default


def _require(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required setting: {name} (or {name}_FILE)")
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
    database_url: str | None = field(default=None, repr=False)        # PostgreSQL, app role
    database_admin_url: str | None = field(default=None, repr=False)  # PostgreSQL, owner (CLI only)
    anthropic_api_key: str | None = field(default=None, repr=False)
    # web address people use, e.g. https://invoices.example.com (links in emails, SSO redirects)
    public_url: str = "http://127.0.0.1:8000"
    force_https: bool = False            # redirect http to https and send HSTS
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_security: str = "starttls"      # starttls | ssl | none
    mail_from: str = "Invoice Review <no-reply@localhost>"
    mail_dir: str | None = None          # development: write emails to this folder instead
    sso_providers: dict = field(default_factory=dict, repr=False)
    oidc_allow_http: bool = False        # testing only: allow a non-HTTPS identity provider

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host or self.mail_dir)

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
            database_url=env("DATABASE_URL"),
            database_admin_url=env("DATABASE_ADMIN_URL"),
            anthropic_api_key=env("ANTHROPIC_API_KEY"),
            public_url=(env("PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/"),
            force_https=env("FORCE_HTTPS", "0") == "1",
            smtp_host=env("SMTP_HOST"),
            smtp_port=int(env("SMTP_PORT", "587")),
            smtp_user=env("SMTP_USER"),
            smtp_password=env("SMTP_PASSWORD"),
            smtp_security=env("SMTP_SECURITY", "starttls"),
            mail_from=env("MAIL_FROM", "Invoice Review <no-reply@localhost>"),
            mail_dir=env("MAIL_DIR"),
            sso_providers=_providers(),
            oidc_allow_http=env("OIDC_ALLOW_HTTP", "0") == "1",
        )


def _providers() -> dict:
    from .oidc import providers_from_env
    names = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET",
             "MICROSOFT_TENANT_ID", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_LABEL",
             "OIDC_REQUIRE_VERIFIED_EMAIL")
    return providers_from_env({n: v for n in names if (v := env(n))})
