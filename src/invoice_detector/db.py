"""Multi-company storage on SQLite or PostgreSQL.

- Every query is parameterized, and every query is scoped to one org_id.
- On PostgreSQL each transaction also sets `app.org_id`, and row-level security
  (see schema.py) enforces the same boundary inside the database.
- Identifiers (sort columns) go through an allow-list.

This uses one connection guarded by a lock, which is fine for a small deployment.
Use a connection pool (e.g. psycopg_pool) when you need more throughput.
"""
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import Settings
from .crypto import Vault
from .detection import Anomaly, Invoice
from .decisions import ACTIONS, suggested_action
from .schema import ADDED_COLUMNS, POSTGRES_SECURITY, POSTGRES_TABLES, SQLITE_SCHEMA
from .security import RateLimitError, hash_password

SORTABLE = {"id", "vendor", "amount", "invoice_date"}  # column names can't be parameters
ROLES = ("admin", "accountant", "auditor")
VERDICTS = ("confirmed", "dismissed")


class Conflict(Exception):
    """A uniqueness, foreign-key or check constraint was violated."""


# For display and totals a credit note counts as a negative amount (it reduces what is
# owed); in the table its amount is positive and its kind says what it is.
SIGNED_AMOUNT = "CASE WHEN kind = 'credit' THEN -amount ELSE amount END AS amount, kind, reference"


class KeyMismatch(RuntimeError):
    """The encryption keys in the settings are not the ones this database was written with."""


SSO_ONLY = "!sso"  # stored instead of a password hash: this user signs in with single sign-on only
SECURITY_FIELDS = ("totp_secret", "totp_enabled", "totp_last_step", "recovery_codes")


def normalize_account(value: str) -> str:
    return "".join(value.split()).upper()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, url: str, keys: dict[int, bytes], index_key: bytes):
        self._keys, self._index_key = keys, index_key
        self._vaults: dict[str, Vault] = {}
        self._lock = threading.RLock()
        self.is_postgres = url.startswith(("postgres://", "postgresql://"))
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row
            self._integrity_error = psycopg.IntegrityError
            self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
        else:
            self._integrity_error = sqlite3.IntegrityError
            self.conn = sqlite3.connect(url, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._check_not_old_format()
            self.conn.executescript(SQLITE_SCHEMA)
            self._add_missing_columns()

    @classmethod
    def from_settings(cls, settings: Settings, admin: bool = False) -> "Database":
        """admin=True uses the owner connection (CLI: migrate, create companies).
        The web server always uses the limited app role."""
        url = settings.database_url or settings.db_path
        if admin and settings.database_admin_url:
            url = settings.database_admin_url
        return cls(url, settings.encryption_keys, settings.index_key)

    def _check_not_old_format(self) -> None:
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(invoices)")]
        if cols and "org_id" not in cols:
            self.conn.close()
            raise RuntimeError("This database file is from an older version without companies. "
                               "Delete it (or set DB_PATH to a new file) and load your data again.")

    def _add_missing_columns(self) -> None:
        """Upgrade a SQLite file made by an older version: add any newer columns."""
        for table, column, definition in ADDED_COLUMNS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        """PostgreSQL only: create tables, the app role, grants and RLS policies.
        Run with the owner connection. If APP_DB_PASSWORD is set, it becomes the
        app role's password."""
        if not self.is_postgres:
            return
        from psycopg import sql
        with self._lock, self.conn.transaction():
            # Several servers may start at once and all run migrate: one at a time, please.
            self.conn.execute("SELECT pg_advisory_xact_lock(7272740001)")
            self.conn.execute(POSTGRES_TABLES)
            for table, column, definition in ADDED_COLUMNS:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            self.conn.execute(POSTGRES_SECURITY)
            from .config import env
            password = env("APP_DB_PASSWORD")
            if password:
                self.conn.execute(sql.SQL("ALTER ROLE invoice_app PASSWORD {}").format(
                    sql.Literal(password)))

    def limiter(self, name: str, max_attempts: int = 5, window: int = 900, lockout: int = 900) -> "DbRateLimiter":
        """A rate limiter whose counts live in this database, shared by every server."""
        return DbRateLimiter(self, name, max_attempts, window, lockout)

    # ----- plumbing -----
    @contextmanager
    def _tx(self, org_id: str | None = None):
        """One transaction, scoped to one company. Yields run(sql, params) -> list[dict]."""
        def run(query: str, params: tuple = ()) -> list[dict]:
            cur = self.conn.execute(query.replace("?", "%s") if self.is_postgres else query, params)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

        with self._lock:
            try:
                if self.is_postgres:
                    with self.conn.transaction():
                        # is_local=true: the setting ends with the transaction, so one
                        # request's company can never leak into the next.
                        self.conn.execute("SELECT set_config('app.org_id', %s, true)", (org_id or "",))
                        yield run
                else:
                    with self.conn:
                        yield run
            except self._integrity_error as exc:
                raise Conflict(str(exc)) from exc

    def _execute(self, query: str, params: tuple = ()):
        """Run one statement inside an open _tx() and return the cursor (for rowcount)."""
        return self.conn.execute(query.replace("?", "%s") if self.is_postgres else query, params)

    def check_keys(self) -> None:
        """Refuse to run with the wrong encryption keys. A mix-up (a lost .env, a restored
        backup with new keys) would otherwise make bank details unreadable or, worse, write
        new data that the real keys can't read. The first run records a check value; later
        runs must be able to decrypt it. Existing encrypted data is tested too, where the
        connection is allowed to see it."""
        master = Vault(self._keys, self._index_key)
        with self._tx() as run:
            rows = run("SELECT value FROM meta WHERE key = ?", ("key_check",))
            if rows:
                stored = json.loads(rows[0]["value"])
                try:
                    ok = master.decrypt(stored["enc"], "meta:key_check") == "ok"
                except ValueError:
                    ok = False
                if not ok or stored.get("idx") != master.blind_index("index-check"):
                    raise KeyMismatch(
                        "The encryption keys don't match this database. Use the ENCRYPTION_KEYS and "
                        "INDEX_KEY it was created with (check your secrets manager or key backup).")
                return
            sample = run("SELECT org_id, id, bank_enc FROM invoices WHERE bank_enc IS NOT NULL LIMIT 1")
        if sample:
            row = sample[0]
            try:
                self.vault(row["org_id"]).decrypt(row["bank_enc"], f"invoices.bank:{row['org_id']}:{row['id']}")
            except ValueError:
                raise KeyMismatch("The encryption keys can't read the bank details already stored in "
                                  "this database. Use the keys it was created with.") from None
        value = json.dumps({"enc": master.encrypt("ok", "meta:key_check"),
                            "idx": master.blind_index("index-check")})
        with self._tx() as run:
            run("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING", ("key_check", value))

    def ping(self) -> bool:
        with self._tx() as run:
            return run("SELECT 1 AS ok")[0]["ok"] == 1

    def vault(self, org_id: str) -> Vault:
        if org_id not in self._vaults:
            self._vaults[org_id] = Vault.for_org(self._keys, self._index_key, org_id)
        return self._vaults[org_id]

    # ----- companies (owner connection on PostgreSQL) -----
    def create_org(self, name: str, approval_threshold: float = 10_000.0) -> str:
        org_id = uuid.uuid4().hex
        with self._tx() as run:
            run("INSERT INTO orgs (id, name, approval_threshold) VALUES (?, ?, ?)",
                (org_id, name, approval_threshold))
        return org_id

    def list_orgs(self) -> list[dict]:
        with self._tx() as run:
            return run("SELECT id, name, approval_threshold FROM orgs ORDER BY name")

    def org_id_by_name(self, name: str) -> str:
        with self._tx() as run:
            rows = run("SELECT id FROM orgs WHERE name = ?", (name,))
        if not rows:
            raise KeyError(name)
        return rows[0]["id"]

    # ----- company settings -----
    def get_org(self, org_id: str) -> dict:
        with self._tx(org_id) as run:
            rows = run("SELECT id, name, approval_threshold, require_2fa, ai_extraction FROM orgs WHERE id = ?",
                       (org_id,))
        if not rows:
            raise KeyError(org_id)
        org = rows[0]
        org["require_2fa"] = bool(org["require_2fa"])
        org["ai_extraction"] = bool(org["ai_extraction"])
        return org

    def set_org_settings(self, org_id: str, approval_threshold: float | None = None,
                         require_2fa: bool | None = None, ai_extraction: bool | None = None) -> None:
        with self._tx(org_id) as run:
            if ai_extraction is not None:
                run("UPDATE orgs SET ai_extraction = ? WHERE id = ?", (int(ai_extraction), org_id))
            if approval_threshold is not None:
                run("UPDATE orgs SET approval_threshold = ? WHERE id = ?", (approval_threshold, org_id))
            if require_2fa is not None:
                run("UPDATE orgs SET require_2fa = ? WHERE id = ?", (int(require_2fa), org_id))

    def set_approval_threshold(self, org_id: str, value: float) -> None:
        self.set_org_settings(org_id, approval_threshold=value)

    # ----- users -----
    def create_user(self, org_id: str, username: str, password: str | None, role: str) -> None:
        """password None makes a single-sign-on-only user (no password can ever match)."""
        with self._tx(org_id) as run:
            run("INSERT INTO users (username, org_id, password_hash, role) VALUES (?, ?, ?, ?)",
                (username, org_id, hash_password(password) if password else SSO_ONLY, role))

    def set_password(self, username: str, password: str, org_id: str | None = None) -> bool:
        """Replace a user's password. False if no such user (in this company, when given;
        the CLI's owner connection may leave the company out)."""
        with self._tx(org_id):
            if org_id is None:
                cur = self._execute("UPDATE users SET password_hash = ? WHERE username = ?",
                                    (hash_password(password), username))
            else:
                cur = self._execute("UPDATE users SET password_hash = ? WHERE username = ? AND org_id = ?",
                                    (hash_password(password), username, org_id))
            return cur.rowcount == 1

    def user_security(self, org_id: str, username: str) -> dict | None:
        """Password hash and two-factor state of one user in this company."""
        with self._tx(org_id) as run:
            rows = run("SELECT username, role, password_hash, totp_secret, totp_enabled, totp_last_step, "
                       "recovery_codes, session_epoch, disabled FROM users WHERE org_id = ? AND username = ?",
                       (org_id, username))
        if not rows:
            return None
        user = rows[0]
        user["totp_enabled"] = bool(user["totp_enabled"])
        user["disabled"] = bool(user["disabled"])
        user["recovery_codes"] = json.loads(user["recovery_codes"]) if user["recovery_codes"] else []
        return user

    def claim_totp_step(self, org_id: str, username: str, step: int) -> bool:
        """Mark a two-factor code's time-step as used, only if no code at or after it has
        been used. One statement, so two requests with the same code can't both succeed."""
        with self._tx(org_id):
            cur = self._execute("UPDATE users SET totp_last_step = ? WHERE org_id = ? AND username = ? "
                                "AND (totp_last_step IS NULL OR totp_last_step < ?)", (step, org_id, username, step))
            return cur.rowcount == 1

    def swap_recovery_codes(self, org_id: str, username: str, before: list[str], after: list[str]) -> bool:
        """Replace the stored recovery codes only if they are still what we read, so one
        recovery code can't be spent twice by two simultaneous requests."""
        with self._tx(org_id):
            cur = self._execute("UPDATE users SET recovery_codes = ? WHERE org_id = ? AND username = ? "
                                "AND recovery_codes = ?", (json.dumps(after), org_id, username, json.dumps(before)))
            return cur.rowcount == 1

    def end_sessions(self, org_id: str, username: str) -> None:
        """Sign this person out everywhere: tokens carry the epoch, so older ones stop working."""
        with self._tx(org_id):
            self._execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE org_id = ? AND username = ?",
                          (org_id, username))

    def set_user_disabled(self, org_id: str, username: str, disabled: bool) -> bool:
        with self._tx(org_id):
            cur = self._execute("UPDATE users SET disabled = ?, session_epoch = session_epoch + 1 "
                                "WHERE org_id = ? AND username = ?", (int(disabled), org_id, username))
            return cur.rowcount == 1

    def active_admins(self, org_id: str) -> int:
        with self._tx(org_id) as run:
            return run("SELECT count(*) AS n FROM users WHERE org_id = ? AND role = 'admin' AND disabled = 0",
                       (org_id,))[0]["n"]

    def update_user_security(self, org_id: str, username: str, **fields) -> None:
        """Change two-factor fields. Only the allow-listed column names can be set."""
        unknown = set(fields) - set(SECURITY_FIELDS)
        if unknown:
            raise ValueError(f"Not a security field: {sorted(unknown)}")
        values = {k: (json.dumps(v) if k == "recovery_codes" and v is not None else
                      int(v) if k == "totp_enabled" else v) for k, v in fields.items()}
        assignments = ", ".join(f"{k} = ?" for k in values)  # names come from the allow-list
        with self._tx(org_id):
            self._execute(f"UPDATE users SET {assignments} WHERE org_id = ? AND username = ?",
                          (*values.values(), org_id, username))

    def auth_lookup(self, username: str) -> dict | None:
        """Used only by login, before the company is known."""
        with self._tx() as run:
            if self.is_postgres:
                rows = run("SELECT username, password_hash, role, org_id, totp_enabled, session_epoch, disabled "
                           "FROM auth_lookup(?)", (username,))
            else:  # case-insensitive, so Alice@Acme.com and alice@acme.com are the same person
                rows = run("SELECT username, password_hash, role, org_id, totp_enabled, session_epoch, disabled "
                           "FROM users WHERE lower(username) = lower(?) ORDER BY username LIMIT 1", (username,))
        if not rows:
            return None
        user = rows[0]
        user["totp_enabled"] = bool(user["totp_enabled"])
        user["disabled"] = bool(user["disabled"])
        return user

    def list_users(self, org_id: str) -> list[dict]:
        with self._tx(org_id) as run:
            rows = run("SELECT username, role, totp_enabled, password_hash, disabled FROM users WHERE org_id = ? "
                       "ORDER BY username", (org_id,))
        return [{"username": r["username"], "role": r["role"], "two_factor": bool(r["totp_enabled"]),
                 "sso_only": r["password_hash"] == SSO_ONLY, "disabled": bool(r["disabled"])} for r in rows]

    # ----- invoices -----
    def add_invoice(self, org_id: str, inv_id: str, vendor: str, amount: float,
                    invoice_date: str, bank_account: str | None, created_by: str, kind: str = "invoice",
                    reference: str | None = None) -> None:
        bank_enc = bank_idx = None
        if bank_account:
            vault = self.vault(org_id)
            bank_enc = vault.encrypt(bank_account, f"invoices.bank:{org_id}:{inv_id}")
            bank_idx = vault.blind_index(normalize_account(bank_account))
        with self._tx(org_id) as run:
            run("INSERT INTO invoices (org_id, id, vendor, amount, invoice_date, bank_enc, "
                "bank_idx, created_by, kind, reference) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (org_id, inv_id, vendor, amount, invoice_date, bank_enc, bank_idx, created_by, kind,
                 _reference(kind, reference)))

    def add_invoices(self, org_id: str, invoices: list[dict], created_by: str) -> list[str]:
        """Insert many invoices in one transaction. Returns the ids that were already
        stored (and so were skipped); everything else was added."""
        vault = self.vault(org_id)
        rows = []
        for inv in invoices:
            bank = inv.get("bank_account")
            rows.append((org_id, inv["id"], inv["vendor"], inv["amount"], inv["date"],
                         vault.encrypt(bank, f"invoices.bank:{org_id}:{inv['id']}") if bank else None,
                         vault.blind_index(normalize_account(bank)) if bank else None, created_by,
                         inv.get("kind") or "invoice", _reference(inv.get("kind"), inv.get("reference"))))
        existing = []
        query = ("INSERT INTO invoices (org_id, id, vendor, amount, invoice_date, bank_enc, bank_idx, "
                 "created_by, kind, reference) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (org_id, id) DO NOTHING")
        if self.is_postgres:
            query = query.replace("?", "%s")
        with self._tx(org_id):
            for row in rows:
                if self.conn.execute(query, row).rowcount == 0:
                    existing.append(row[1])
        return existing

    def get_invoices(self, org_id: str, ids: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ", ".join("?" * len(chunk))
            with self._tx(org_id) as run:
                for r in run(f"SELECT id, vendor, {SIGNED_AMOUNT}, invoice_date FROM invoices "
                             f"WHERE org_id = ? AND id IN ({marks})", (org_id, *chunk)):
                    found[r["id"]] = r
        return found

    def count_invoices(self, org_id: str) -> int:
        with self._tx(org_id) as run:
            return run("SELECT count(*) AS n FROM invoices WHERE org_id = ?", (org_id,))[0]["n"]

    def last_audit(self, org_id: str, action: str) -> dict | None:
        with self._tx(org_id) as run:
            rows = run("SELECT at, username, detail FROM audit_log WHERE org_id = ? AND action = ? "
                       "ORDER BY id DESC LIMIT 1", (org_id, action))
        return rows[0] if rows else None

    def list_invoices(self, org_id: str, sort: str = "invoice_date", limit: int = 50) -> list[dict]:
        if sort not in SORTABLE:
            raise ValueError("Invalid sort column")
        limit = max(1, min(int(limit), 500))
        with self._tx(org_id) as run:  # bank details are never returned here
            return run(f"SELECT id, vendor, {SIGNED_AMOUNT}, invoice_date, "
                       "(SELECT p.paid_at FROM payments p WHERE p.org_id = invoices.org_id "
                       " AND p.invoice_id = invoices.id) AS paid_at FROM invoices "
                       f"WHERE org_id = ? ORDER BY {sort}, id LIMIT ?", (org_id, limit))

    def get_bank_account(self, org_id: str, inv_id: str) -> str | None:
        with self._tx(org_id) as run:
            rows = run("SELECT bank_enc FROM invoices WHERE org_id = ? AND id = ?", (org_id, inv_id))
        if not rows:
            raise KeyError(inv_id)
        enc = rows[0]["bank_enc"]
        return self.vault(org_id).decrypt(enc, f"invoices.bank:{org_id}:{inv_id}") if enc else None

    def detection_inputs(self, org_id: str) -> list[Invoice]:
        with self._tx(org_id) as run:
            rows = run("SELECT i.id, i.vendor, i.amount, i.invoice_date, i.bank_idx, d.totals_issue, i.kind, i.reference, "
                       "p.paid_at FROM invoices i "
                       "LEFT JOIN documents d ON d.org_id = i.org_id AND d.invoice_id = i.id "
                       "LEFT JOIN payments p ON p.org_id = i.org_id AND p.invoice_id = i.id "
                       "WHERE i.org_id = ?", (org_id,))
        return [Invoice(r["id"], r["vendor"], r["amount"], r["invoice_date"], r["bank_idx"], r["totals_issue"],
                        r["kind"], r["reference"], (r["paid_at"] or "")[:10] or None) for r in rows]

    # ----- invoice documents -----
    def document_by_hash(self, org_id: str, sha256: str) -> dict | None:
        with self._tx(org_id) as run:
            rows = run("SELECT filename, invoice_id, uploaded_by, uploaded_at FROM documents "
                       "WHERE org_id = ? AND sha256 = ?", (org_id, sha256))
        return rows[0] if rows else None

    def vendor_names(self, org_id: str) -> list[str]:
        with self._tx(org_id) as run:
            return [r["vendor"] for r in run("SELECT DISTINCT vendor FROM invoices WHERE org_id = ?", (org_id,))]

    def add_document_invoices(self, org_id: str, rows: list[dict], created_by: str) -> list[str]:
        """Each row: an invoice plus its document record, saved together in one transaction
        (both or neither). Returns invoice ids skipped because they were already stored."""
        vault = self.vault(org_id)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        skipped = []
        mark = (lambda q: q.replace("?", "%s")) if self.is_postgres else (lambda q: q)
        with self._tx(org_id):
            for r in rows:
                bank = r.get("bank_account")
                inserted = self.conn.execute(mark(
                    "INSERT INTO invoices (org_id, id, vendor, amount, invoice_date, bank_enc, bank_idx, created_by, "
                    "kind, reference) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (org_id, id) DO NOTHING"),
                    (org_id, r["id"], r["vendor"], r["amount"], r["date"],
                     vault.encrypt(bank, f"invoices.bank:{org_id}:{r['id']}") if bank else None,
                     vault.blind_index(normalize_account(bank)) if bank else None, created_by,
                     r.get("kind") or "invoice", _reference(r.get("kind"), r.get("reference")))).rowcount
                if not inserted:
                    skipped.append(r["id"])
                    continue
                self.conn.execute(mark(
                    "INSERT INTO documents (org_id, sha256, filename, method, invoice_id, totals_issue, corrections, "
                    "uploaded_by, uploaded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"),
                    (org_id, r["sha256"], r["filename"], r["method"], r["id"], r.get("totals_issue"),
                     json.dumps(r.get("corrections") or {}), created_by, now))
        return skipped

    # ----- what reviewers taught the document reader -----
    MAX_HINTS = 2000

    def reading_hints(self, org_id: str) -> dict[str, dict[str, list[str]]]:
        """{vendor: {field: [labels, most useful first]}} for labels that helped more than they hurt."""
        with self._tx(org_id) as run:
            rows = run("SELECT vendor, field, label FROM reading_hints WHERE org_id = ? AND hits > misses "
                       "ORDER BY hits - misses DESC, label LIMIT ?", (org_id, self.MAX_HINTS))
        hints: dict[str, dict[str, list[str]]] = {}
        for r in rows:
            hints.setdefault(r["vendor"], {}).setdefault(r["field"], []).append(r["label"])
        return hints

    def learn_reading(self, org_id: str, vendor: str, field: str, label: str, helped: bool) -> None:
        if field not in ("id", "date", "amount") or not vendor or not label:
            return
        vendor, label = vendor[:120], label[:60]
        hit, miss = (1, 0) if helped else (0, 1)
        with self._tx(org_id) as run:
            run("INSERT INTO reading_hints (org_id, vendor, field, label, hits, misses) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (org_id, vendor, field, label) DO UPDATE SET "
                "hits = reading_hints.hits + ?, misses = reading_hints.misses + ?",
                (org_id, vendor, field, label, hit, miss, hit, miss))

    def document_stats(self, org_id: str) -> dict:
        """How well documents are being read: how many, and how many fields people corrected."""
        with self._tx(org_id) as run:
            rows = run("SELECT method, corrections FROM documents WHERE org_id = ?", (org_id,))
        corrected = sum(len(json.loads(r["corrections"] or "{}")) for r in rows)
        by_method: dict[str, int] = {}
        for r in rows:
            by_method[r["method"]] = by_method.get(r["method"], 0) + 1
        return {"documents": len(rows), "fields_corrected": corrected, "by_method": by_method}

    # ----- flags -----
    def save_flags(self, org_id: str, anomalies: list[Anomaly]) -> None:
        with self._tx(org_id) as run:
            if self.is_postgres:   # two servers detecting for the same company: one after the other
                run("SELECT pg_advisory_xact_lock(hashtext(?))", ("flags:" + org_id,))
            run("DELETE FROM anomalies WHERE org_id = ?", (org_id,))
            for a in anomalies:
                run("INSERT INTO anomalies (org_id, invoice_id, rule, severity, detail) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (org_id, a.invoice_id, a.rule, a.severity, a.detail))

    def list_anomalies(self, org_id: str, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        with self._tx(org_id) as run:
            return run("SELECT invoice_id, rule, severity, detail FROM anomalies WHERE org_id = ? "
                       "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                       "invoice_id, rule LIMIT ?", (org_id, limit))

    # ----- reviewer feedback -----
    def set_review(self, org_id: str, invoice_id: str, verdict: str, reviewer: str,
                   action: str | None = None, note: str | None = None) -> str:
        """Record a reviewer's decision, with a snapshot of the rules that fired, and the
        payment action that follows from it. Confirming without choosing an action applies
        the suggested one (e.g. 'do_not_pay' for a duplicate); dismissing means 'pay'.
        Returns the action stored."""
        if verdict not in VERDICTS:
            raise ValueError("Invalid verdict")
        if action is not None and action not in ACTIONS:
            raise ValueError("Invalid action")
        with self._tx(org_id) as run:
            rules = [r["rule"] for r in run(
                "SELECT rule FROM anomalies WHERE org_id = ? AND invoice_id = ? ORDER BY rule",
                (org_id, invoice_id))]
            if not rules:
                raise KeyError(invoice_id)
            if verdict == "dismissed":
                action = "pay"
            elif action is None:
                action = suggested_action(rules)
            # A new review clears any second approval: it was given to the old decision.
            run("INSERT INTO reviews (org_id, invoice_id, verdict, action, note, rules, reviewer, reviewed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (org_id, invoice_id) DO UPDATE SET verdict = excluded.verdict, "
                "action = excluded.action, note = excluded.note, rules = excluded.rules, "
                "reviewer = excluded.reviewer, reviewed_at = excluded.reviewed_at, "
                "approved_by = NULL, approved_at = NULL",
                (org_id, invoice_id, verdict, action, (note or "").strip()[:500] or None,
                 ",".join(rules), reviewer, _now()))
        return action

    def review_details(self, org_id: str) -> dict[str, dict]:
        """{invoice id: {verdict, action, note, reviewer, reviewed_at, approved_by, approved_at}}."""
        with self._tx(org_id) as run:
            rows = run("SELECT invoice_id, verdict, action, note, reviewer, reviewed_at, approved_by, approved_at "
                       "FROM reviews WHERE org_id = ?", (org_id,))
        return {r.pop("invoice_id"): r for r in rows}

    def approve_review(self, org_id: str, invoice_id: str, approver: str) -> None:
        with self._tx(org_id) as run:
            run("UPDATE reviews SET approved_by = ?, approved_at = ? WHERE org_id = ? AND invoice_id = ?",
                (approver, _now(), org_id, invoice_id))

    def invoice_facts(self, org_id: str, invoice_id: str) -> dict | None:
        """What the controls need: amount (positive), kind, who added it, whether it's paid."""
        with self._tx(org_id) as run:
            rows = run("SELECT i.amount, i.kind, i.created_by, p.paid_at FROM invoices i "
                       "LEFT JOIN payments p ON p.org_id = i.org_id AND p.invoice_id = i.id "
                       "WHERE i.org_id = ? AND i.id = ?", (org_id, invoice_id))
        return rows[0] if rows else None

    def count_reviewers(self, org_id: str) -> int:
        with self._tx(org_id) as run:
            rows = run("SELECT COUNT(*) AS n FROM users WHERE org_id = ? AND disabled = 0 "
                       "AND role IN ('admin', 'accountant')", (org_id,))
        return int(rows[0]["n"])

    # ----- payments -----
    def mark_paid(self, org_id: str, invoice_ids: list[str], paid_by: str, reference: str | None = None) -> list[str]:
        """Record payment of these invoices. Returns the ids newly marked (already paid ones are skipped)."""
        marked = []
        with self._tx(org_id) as run:
            for inv_id in invoice_ids:
                if run("SELECT 1 FROM payments WHERE org_id = ? AND invoice_id = ?", (org_id, inv_id)):
                    continue
                run("INSERT INTO payments (org_id, invoice_id, paid_at, paid_by, reference) VALUES (?, ?, ?, ?, ?)",
                    (org_id, inv_id, _now(), paid_by, (reference or "").strip()[:120] or None))
                marked.append(inv_id)
        return marked

    def unmark_paid(self, org_id: str, invoice_id: str) -> bool:
        with self._tx(org_id) as run:
            found = run("SELECT 1 FROM payments WHERE org_id = ? AND invoice_id = ?", (org_id, invoice_id))
            if found:
                run("DELETE FROM payments WHERE org_id = ? AND invoice_id = ?", (org_id, invoice_id))
        return bool(found)

    def payments(self, org_id: str) -> dict[str, dict]:
        """{invoice id: {paid_at, paid_by, reference}}"""
        with self._tx(org_id) as run:
            rows = run("SELECT invoice_id, paid_at, paid_by, reference FROM payments WHERE org_id = ?", (org_id,))
        return {r.pop("invoice_id"): r for r in rows}

    def all_invoices(self, org_id: str) -> list[dict]:
        """Every invoice of this company (no bank data), oldest first, with paid_at if paid."""
        with self._tx(org_id) as run:
            return run(f"SELECT i.id, i.vendor, {_qualified(SIGNED_AMOUNT)}, i.invoice_date, i.created_by, p.paid_at "
                       "FROM invoices i LEFT JOIN payments p ON p.org_id = i.org_id AND p.invoice_id = i.id "
                       "WHERE i.org_id = ? ORDER BY i.invoice_date, i.id", (org_id,))

    def list_reviews(self, org_id: str) -> dict[str, str]:
        with self._tx(org_id) as run:
            rows = run("SELECT invoice_id, verdict FROM reviews WHERE org_id = ?", (org_id,))
        return {r["invoice_id"]: r["verdict"] for r in rows}

    def feedback_counts(self, org_id: str) -> dict[str, tuple[int, int]]:
        """{rule: (confirmed, dismissed)} from this company's reviews."""
        with self._tx(org_id) as run:
            rows = run("SELECT verdict, rules FROM reviews WHERE org_id = ?", (org_id,))
        counts: dict[str, list[int]] = {}
        for r in rows:
            for rule in r["rules"].split(","):
                counts.setdefault(rule, [0, 0])[0 if r["verdict"] == "confirmed" else 1] += 1
        return {rule: (c, d) for rule, (c, d) in counts.items()}

    # ----- audit trail (append-only for the app role on PostgreSQL) -----
    def audit(self, org_id: str, username: str, action: str, detail: str = "") -> None:
        with self._tx(org_id) as run:
            run("INSERT INTO audit_log (org_id, at, username, action, detail) VALUES (?, ?, ?, ?, ?)",
                (org_id, _now(), username, action, detail))

    def list_audit(self, org_id: str, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._tx(org_id) as run:
            return run("SELECT at, username, action, detail FROM audit_log WHERE org_id = ? "
                       "ORDER BY id DESC LIMIT ?", (org_id, limit))


def _qualified(columns: str) -> str:
    """SIGNED_AMOUNT with the invoices table named, for queries that join other tables."""
    return (columns.replace("kind = 'credit'", "i.kind = 'credit'").replace("-amount", "-i.amount")
            .replace("ELSE amount", "ELSE i.amount").replace("AS amount, kind, reference", "AS amount, i.kind, i.reference"))


def _reference(kind: str | None, reference) -> str | None:
    """Only a credit note names another invoice; keep it short and plain."""
    if kind != "credit" or reference in (None, ""):
        return None
    return " ".join(str(reference).split())[:64] or None


class DbRateLimiter:
    """Count failures per key in the database; after max_attempts within the window, lock
    the key out. Same interface as security.RateLimiter, but the count is shared by every
    server and survives restarts, so adding servers doesn't multiply an attacker's guesses.

    Each operation is one SQL statement (an upsert with the arithmetic inside it), so two
    servers recording a failure at the same moment both count."""

    def __init__(self, db: Database, name: str, max_attempts: int = 5, window: int = 900, lockout: int = 900):
        self.db, self.name = db, name
        self.max, self.window, self.lockout = max_attempts, window, lockout

    def _key(self, key: str) -> str:
        # Keyed, so a copy of the table doesn't reveal which usernames or addresses were tried.
        return hmac.new(self.db._index_key, f"{self.name}:{key}".encode(), hashlib.sha256).hexdigest()

    def check(self, key: str, now: float | None = None) -> None:
        now = int(time.time() if now is None else now)
        with self.db._tx() as run:
            rows = run("SELECT locked_until FROM rate_limits WHERE key = ?", (self._key(key),))
        if rows and rows[0]["locked_until"] > now:
            raise RateLimitError(int(rows[0]["locked_until"] - now) + 1)

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = int(time.time() if now is None else now)
        expired = now - self.window
        with self.db._tx() as run:
            # A stale window starts again at 1; otherwise add one. Reaching the maximum locks.
            run("INSERT INTO rate_limits (key, failures, window_start, locked_until) VALUES (?, 1, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET "
                "failures = CASE WHEN rate_limits.window_start < ? THEN 1 ELSE rate_limits.failures + 1 END, "
                "window_start = CASE WHEN rate_limits.window_start < ? THEN ? ELSE rate_limits.window_start END, "
                "locked_until = CASE WHEN (CASE WHEN rate_limits.window_start < ? THEN 1 "
                "ELSE rate_limits.failures + 1 END) >= ? THEN ? ELSE rate_limits.locked_until END",
                (self._key(key), now, now + self.lockout if self.max <= 1 else 0,
                 expired, expired, now, expired, self.max, now + self.lockout))
            if secrets.randbelow(200) == 0:   # now and then, clear out long-finished entries
                run("DELETE FROM rate_limits WHERE window_start < ? AND locked_until < ?", (now - 86400, now))

    def record_success(self, key: str) -> None:
        with self.db._tx() as run:
            run("DELETE FROM rate_limits WHERE key = ? AND locked_until <= ?", (self._key(key), int(time.time())))
