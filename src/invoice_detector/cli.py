"""Command line helpers. COMPANY is the company name, in quotes if it has spaces.

  python -m invoice_detector.cli init                           # create .env with fresh secrets
  python -m invoice_detector.cli migrate                        # PostgreSQL only: tables + row-level security
  python -m invoice_detector.cli create-org COMPANY             # add a company
  python -m invoice_detector.cli list-orgs
  python -m invoice_detector.cli create-user COMPANY NAME ROLE  # role: admin|accountant|auditor
  python -m invoice_detector.cli reset-password NAME            # set a new password for a user
  python -m invoice_detector.cli seed COMPANY                   # load synthetic demo invoices
  python -m invoice_detector.cli import COMPANY FILE            # Excel (.xlsx) or CSV; columns detected automatically
  python -m invoice_detector.cli detect COMPANY                 # run detection, print the review queue
  python -m invoice_detector.cli evaluate                       # score the detector on synthetic data
  python -m invoice_detector.cli evaluate-documents [N]         # score invoice-document reading
  python -m invoice_detector.cli gen-keys                       # print fresh secrets (for a secrets manager)
  python -m invoice_detector.cli init-deploy DIR                # secret files for docker-compose.prod.yml
"""
import base64
import getpass
import os
import secrets
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

from .config import Settings
from .db import ROLES, Conflict, Database, KeyMismatch
from .demo import generate_demo
from .detection import DEFAULT_CONFIG, detect, learned_weights, review_queue
from .evaluate import evaluate, format_report
from .importer import FileProblem, convert, preview, read_table


@contextmanager
def _db(check_keys: bool = True):
    """The CLI is an operator tool, so it uses the owner connection when one is set.
    It refuses to work with encryption keys that don't match the database."""
    db = Database.from_settings(Settings.from_env(), admin=True)
    try:
        if check_keys:
            db.check_keys()
        yield db
    finally:
        db.close()


def _fresh_secrets() -> str:
    def key() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    return (f"SECRET_KEY={secrets.token_urlsafe(48)}\n"
            f"ENCRYPTION_KEYS=1:{key()}\n"
            f"INDEX_KEY={key()}\n")


def _init_deploy(folder: str) -> int:
    """One file per secret, as Docker secrets expect, with matching database passwords."""
    def key() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()
    owner, app_pw = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    files = {
        "secret_key": secrets.token_urlsafe(48),
        "encryption_keys": f"1:{key()}",
        "index_key": key(),
        "postgres_password": owner,
        "app_db_password": app_pw,
        "database_url": f"postgresql://invoice_app:{app_pw}@db:5432/invoices",
        "database_admin_url": f"postgresql://postgres:{owner}@db:5432/invoices",
    }
    existing = [n for n in files if os.path.exists(os.path.join(folder, n))]
    if existing:
        print(f"{folder} already has secrets ({', '.join(existing)}); leaving them alone. "
              "Replacing encryption keys would make stored bank details unreadable.")
        return 1
    # The folder is private to you (700). The files inside are 644 so the app container, which
    # runs as its own non-root user, can read them where Docker mounts them; nobody else on the
    # server can reach them, because they can't enter the folder.
    os.makedirs(folder, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    for name, value in files.items():
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(value + "\n")
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    print(f"Wrote {len(files)} secret files to {folder}.")
    print("Back up this folder now (a password manager or an offline copy). Without encryption_keys "
          "and index_key, the bank details in your database and backups can never be read again.")
    return 0


def _init() -> int:
    if os.path.exists(".env"):
        print(".env already exists; leaving it alone. (Deleting it makes encrypted data unreadable.)")
        return 1
    with open(".env", "w", encoding="utf-8", newline="\n") as f:
        f.write(_fresh_secrets() + "DB_PATH=invoices.db\n")
    print("Created .env with fresh secrets. Never commit this file.")
    return 0


def _org(db: Database, name: str) -> str | None:
    try:
        return db.org_id_by_name(name)
    except KeyError:
        print(f"No company named '{name}'. Create it with: create-org \"{name}\"")
        return None


def _create_user(company: str, name: str, role: str) -> int:
    if role not in ROLES:
        print(f"Role must be one of: {', '.join(ROLES)}")
        return 1
    with _db() as db:
        org_id = _org(db, company)
        if org_id is None:
            return 1
        password = getpass.getpass("Password (min 12 chars, nothing shows while you type): ")
        if len(password) < 12:
            print("Password too short.")
            return 1
        try:
            db.create_user(org_id, name, password, role)
        except Conflict:
            print(f"Username '{name}' is already taken.")
            return 1
    print(f"Created {role} user '{name}' in {company}.")
    return 0


def _reset_password(name: str) -> int:
    password = getpass.getpass("New password (min 12 chars, nothing shows while you type): ")
    if len(password) < 12:
        print("Password too short.")
        return 1
    with _db() as db:
        if not db.set_password(name, password):
            print(f"No user called '{name}'.")
            return 1
    print(f"Password changed for '{name}'.")
    return 0


def _load(db: Database, org_id: str, rows) -> tuple[int, int]:
    added = skipped = 0
    for r in rows:
        try:
            amount = float(r["amount"])
            date.fromisoformat(r["date"])  # must be yyyy-mm-dd
            db.add_invoice(org_id, r["id"].strip(), r["vendor"].strip(), amount, r["date"],
                           (r.get("bank_account") or "").strip() or None, "import")
            added += 1
        except (KeyError, ValueError, AttributeError, Conflict):
            skipped += 1  # bad row or an id that is already stored
    return added, skipped


def _import(company: str, path: str) -> int:
    try:
        with open(path, "rb") as f:
            table = read_table(path, f.read())
        info = preview(table)
        result = convert(table, info["mapping"], info["date_order"])
    except (OSError, FileProblem) as exc:
        print(f"Could not import {path}: {exc}")
        return 1
    print("Columns used: " + ", ".join(f"{k} <- '{v}'" for k, v in info["mapping"].items() if v))
    if info["date_order_ambiguous"]:
        print("Note: dates like 05/03/2026 were read as month/day/year. If that's wrong, use the "
              "web app's upload page, which lets you choose.")
    with _db() as db:
        org_id = _org(db, company)
        if org_id is None:
            return 1
        existing = db.add_invoices(org_id, result.invoices, "import")
    added = len(result.invoices) - len(existing)
    print(f"Imported {added} invoices into {company}; {len(existing)} were already stored, "
          f"{len(result.errors)} rows had problems.")
    for err in result.errors[:20]:
        print(f"  row {err['row']}: {err['message']}")
    return 0


def _detect(company: str) -> int:
    with _db() as db:
        org_id = _org(db, company)
        if org_id is None:
            return 1
        cfg = replace(DEFAULT_CONFIG, approval_threshold=db.get_org(org_id)["approval_threshold"])
        invoices = db.detection_inputs(org_id)
        found = detect(invoices, cfg)
        db.save_flags(org_id, found)
        queue = review_queue(found, learned_weights(db.feedback_counts(org_id)))
    print(f"{company}: checked {len(invoices)} invoices, {len(queue)} flagged for review.\n")
    for item in queue[:25]:
        print(f"{item['invoice_id']:<12} risk {item['risk']:>3}  {', '.join(item['rules'])}")
    return 0


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "init" and not args:
        return _init()
    if cmd == "init-deploy" and len(args) == 1:
        return _init_deploy(args[0])
    if cmd == "gen-keys" and not args:
        print(_fresh_secrets(), end="")
    elif cmd == "evaluate" and not args:
        print(format_report(evaluate()))
    elif cmd == "evaluate-documents" and len(args) <= 1:
        from .doc_samples import evaluate_documents
        report = evaluate_documents(int(args[0]) if args else 100)
        print(f"Read {report['documents']} generated invoice documents (PDF and Excel, four layouts):")
        for field, acc in report["accuracy"].items():
            print(f"  {field:13s} {acc:.1%}" if acc is not None else f"  {field:13s} -")
        t = report["totals_check"]
        print(f"  totals check  caught {t['caught']} planted errors, missed {t['missed']}, "
              f"{t['false_alarms']} false alarms")
        for m in report["mistakes"][:10]:
            print(f"    {m['file']} ({m['layout']}): {m['field']} expected {m['expected']!r}, read {m['read']!r}")
    elif cmd == "migrate" and not args:
        with _db(check_keys=False) as db:
            db.migrate()
            db.check_keys()
        print("Database is up to date." if db.is_postgres
              else "Nothing to do: SQLite tables are created automatically.")
    elif cmd == "create-org" and len(args) == 1:
        try:
            with _db() as db:
                db.create_org(args[0])
        except Conflict:
            print(f"Company '{args[0]}' already exists.")
            return 1
        print(f"Created company '{args[0]}'.")
    elif cmd == "list-orgs" and not args:
        with _db() as db:
            for org in db.list_orgs():
                print(f"{org['name']}  (approval threshold {org['approval_threshold']:,.0f})")
    elif cmd == "create-user" and len(args) == 3:
        return _create_user(*args)
    elif cmd == "reset-password" and len(args) == 1:
        return _reset_password(args[0])
    elif cmd == "seed" and len(args) == 1:
        with _db() as db:
            org_id = _org(db, args[0])
            if org_id is None:
                return 1
            rows, injected = generate_demo()
            added, skipped = _load(db, org_id, rows)
        print(f"Loaded {added} synthetic invoices into {args[0]} ({skipped} skipped), "
              f"including {len(injected)} injected anomalies.")
    elif cmd in ("import", "import-csv") and len(args) == 2:
        return _import(*args)
    elif cmd == "detect" and len(args) == 1:
        return _detect(args[0])
    else:
        print(__doc__)
        return 1
    return 0


def run(argv: list[str]) -> int:
    """main(), with configuration problems shown as one clear line instead of a traceback."""
    try:
        return main(argv)
    except (RuntimeError, KeyMismatch) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
