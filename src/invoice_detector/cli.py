"""Command line helpers.

  python -m invoice_detector.cli init                   # create .env with fresh secrets
  python -m invoice_detector.cli create-user NAME ROLE  # role: admin|accountant|auditor
  python -m invoice_detector.cli seed                   # load synthetic demo invoices
  python -m invoice_detector.cli import-csv FILE        # columns: id,vendor,amount,date[,bank_account]
  python -m invoice_detector.cli detect                 # run detection, print the review queue
  python -m invoice_detector.cli evaluate               # score the detector on synthetic data
  python -m invoice_detector.cli gen-keys               # print fresh secrets (for a secrets manager)
"""
import base64
import csv
import getpass
import os
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import date

from .config import Settings
from .crypto import Vault
from .db import Database
from .demo import generate_demo
from .detection import detect, review_queue
from .evaluate import evaluate, format_report

ROLES = ("admin", "accountant", "auditor")


@contextmanager
def _db():
    settings = Settings.from_env()
    db = Database(settings.db_path, Vault.from_settings(settings))
    try:
        yield db
    finally:
        db.close()


def _fresh_secrets() -> str:
    def key() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    return (f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
            f"ENCRYPTION_KEYS=1:{key()}\n"
            f"INDEX_KEY={key()}\n")


def _init() -> int:
    if os.path.exists(".env"):
        print(".env already exists; leaving it alone. (Deleting it makes encrypted data unreadable.)")
        return 1
    with open(".env", "w", encoding="utf-8", newline="\n") as f:
        f.write(_fresh_secrets() + "DB_PATH=invoices.db\n")
    print("Created .env with fresh secrets. Never commit this file.")
    return 0


def _create_user(name: str, role: str) -> int:
    if role not in ROLES:
        print(f"Role must be one of: {', '.join(ROLES)}")
        return 1
    password = getpass.getpass("Password (min 12 chars): ")
    if len(password) < 12:
        print("Password too short.")
        return 1
    try:
        with _db() as db:
            db.create_user(name, password, role)
    except sqlite3.IntegrityError:
        print(f"User '{name}' already exists.")
        return 1
    print(f"Created {role} user '{name}'.")
    return 0


def _load(db: Database, rows) -> tuple[int, int]:
    added = skipped = 0
    for r in rows:
        try:
            amount = float(r["amount"])
            date.fromisoformat(r["date"])  # must be yyyy-mm-dd
            db.add_invoice(r["id"].strip(), r["vendor"].strip(), amount, r["date"],
                           (r.get("bank_account") or "").strip() or None, "import")
            added += 1
        except (KeyError, ValueError, AttributeError, sqlite3.IntegrityError):
            skipped += 1  # bad row or an id that is already stored
    return added, skipped


def _detect() -> int:
    with _db() as db:
        invoices = db.detection_inputs()
        found = detect(invoices)
        db.save_flags(found)
    queue = review_queue(found)
    print(f"Checked {len(invoices)} invoices: {len(queue)} flagged for review.\n")
    for item in queue[:25]:
        print(f"{item['invoice_id']:<12} risk {item['risk']:>3}  {', '.join(item['rules'])}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "init":
        return _init()
    if cmd == "gen-keys":
        print(_fresh_secrets(), end="")
    elif cmd == "create-user" and len(argv) == 3:
        return _create_user(argv[1], argv[2])
    elif cmd == "seed":
        rows, injected = generate_demo()
        with _db() as db:
            added, skipped = _load(db, rows)
        print(f"Loaded {added} synthetic invoices ({skipped} skipped), "
              f"including {len(injected)} injected anomalies.")
    elif cmd == "import-csv" and len(argv) == 2:
        with open(argv[1], newline="", encoding="utf-8-sig") as f, _db() as db:
            added, skipped = _load(db, csv.DictReader(f))
        print(f"Imported {added} invoices, skipped {skipped} (bad rows or existing ids).")
    elif cmd == "detect":
        return _detect()
    elif cmd == "evaluate":
        print(format_report(evaluate()))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
