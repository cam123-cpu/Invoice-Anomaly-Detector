"""Command line helpers.

  python -m invoice_detector.cli gen-keys              # print fresh secrets for .env
  python -m invoice_detector.cli create-user NAME ROLE # role: admin|accountant|auditor
  python -m invoice_detector.cli seed                  # load synthetic demo invoices
"""
import base64
import getpass
import secrets
import sys

from .config import Settings
from .crypto import Vault
from .db import Database
from .demo import generate_demo


def _db() -> Database:
    settings = Settings.from_env()
    return Database(settings.db_path, Vault.from_settings(settings))


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "gen-keys":
        b64 = lambda: base64.b64encode(secrets.token_bytes(32)).decode()  # noqa: E731
        print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        print(f"ENCRYPTION_KEYS=1:{b64()}")
        print(f"INDEX_KEY={b64()}")
    elif cmd == "create-user" and len(argv) == 3:
        password = getpass.getpass("Password (min 12 chars): ")
        if len(password) < 12:
            print("Password too short.")
            return 1
        _db().create_user(argv[1], password, argv[2])
        print(f"Created {argv[2]} user '{argv[1]}'.")
    elif cmd == "seed":
        db = _db()
        rows, injected = generate_demo()
        for r in rows:
            db.add_invoice(r["id"], r["vendor"], r["amount"], r["date"], r["bank_account"], "seed")
        print(f"Loaded {len(rows)} invoices. Injected anomalies: {injected}")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
