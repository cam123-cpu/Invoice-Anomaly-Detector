"""Multi-company storage on SQLite or PostgreSQL.

- Every query is parameterized, and every query is scoped to one org_id.
- On PostgreSQL each transaction also sets `app.org_id`, and row-level security
  (see schema.py) enforces the same boundary inside the database.
- Identifiers (sort columns) go through an allow-list.

This uses one connection guarded by a lock, which is fine for a small deployment.
Use a connection pool (e.g. psycopg_pool) when you need more throughput.
"""
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import Settings
from .crypto import Vault
from .detection import Anomaly, Invoice
from .decisions import ACTIONS, suggested_action
from .schema import ADDED_COLUMNS, POSTGRES_SECURITY, POSTGRES_TABLES, SQLITE_SCHEMA
from .security import hash_password

SORTABLE = {"id", "vendor", "amount", "invoice_date"}  # column names can't be parameters
ROLES = ("admin", "accountant", "auditor")
VERDICTS = ("confirmed", "dismissed")


class Conflict(Exception):
    """A uniqueness, foreign-key or check constraint was violated."""


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
            self.conn.execute(POSTGRES_TABLES)
            for table, column, definition in ADDED_COLUMNS:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
            self.conn.execute(POSTGRES_SECURITY)
            password = os.environ.get("APP_DB_PASSWORD")
            if password:
                self.conn.execute(sql.SQL("ALTER ROLE invoice_app PASSWORD {}").format(
                    sql.Literal(password)))

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
            rows = run("SELECT id, name, approval_threshold FROM orgs WHERE id = ?", (org_id,))
        if not rows:
            raise KeyError(org_id)
        return rows[0]

    def set_approval_threshold(self, org_id: str, value: float) -> None:
        with self._tx(org_id) as run:
            run("UPDATE orgs SET approval_threshold = ? WHERE id = ?", (value, org_id))

    # ----- users -----
    def create_user(self, org_id: str, username: str, password: str, role: str) -> None:
        with self._tx(org_id) as run:
            run("INSERT INTO users (username, org_id, password_hash, role) VALUES (?, ?, ?, ?)",
                (username, org_id, hash_password(password), role))

    def set_password(self, username: str, password: str) -> bool:
        """Operator tool (owner connection): replace a user's password. False if no such user."""
        query = "UPDATE users SET password_hash = ? WHERE username = ?"
        with self._tx():
            cur = self.conn.execute(query.replace("?", "%s") if self.is_postgres else query,
                                    (hash_password(password), username))
            return cur.rowcount == 1

    def auth_lookup(self, username: str) -> dict | None:
        """Used only by login, before the company is known."""
        with self._tx() as run:
            if self.is_postgres:
                rows = run("SELECT username, password_hash, role, org_id FROM auth_lookup(?)",
                           (username,))
            else:
                rows = run("SELECT username, password_hash, role, org_id FROM users "
                           "WHERE username = ?", (username,))
        return rows[0] if rows else None

    def list_users(self, org_id: str) -> list[dict]:
        with self._tx(org_id) as run:
            return run("SELECT username, role FROM users WHERE org_id = ? ORDER BY username",
                       (org_id,))

    # ----- invoices -----
    def add_invoice(self, org_id: str, inv_id: str, vendor: str, amount: float,
                    invoice_date: str, bank_account: str | None, created_by: str) -> None:
        bank_enc = bank_idx = None
        if bank_account:
            vault = self.vault(org_id)
            bank_enc = vault.encrypt(bank_account, f"invoices.bank:{org_id}:{inv_id}")
            bank_idx = vault.blind_index(normalize_account(bank_account))
        with self._tx(org_id) as run:
            run("INSERT INTO invoices (org_id, id, vendor, amount, invoice_date, bank_enc, "
                "bank_idx, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (org_id, inv_id, vendor, amount, invoice_date, bank_enc, bank_idx, created_by))

    def add_invoices(self, org_id: str, invoices: list[dict], created_by: str) -> list[str]:
        """Insert many invoices in one transaction. Returns the ids that were already
        stored (and so were skipped); everything else was added."""
        vault = self.vault(org_id)
        rows = []
        for inv in invoices:
            bank = inv.get("bank_account")
            rows.append((org_id, inv["id"], inv["vendor"], inv["amount"], inv["date"],
                         vault.encrypt(bank, f"invoices.bank:{org_id}:{inv['id']}") if bank else None,
                         vault.blind_index(normalize_account(bank)) if bank else None, created_by))
        existing = []
        query = ("INSERT INTO invoices (org_id, id, vendor, amount, invoice_date, bank_enc, bank_idx, "
                 "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (org_id, id) DO NOTHING")
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
                for r in run(f"SELECT id, vendor, amount, invoice_date FROM invoices "
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
            return run(f"SELECT id, vendor, amount, invoice_date FROM invoices "
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
            rows = run("SELECT id, vendor, amount, invoice_date, bank_idx FROM invoices "
                       "WHERE org_id = ?", (org_id,))
        return [Invoice(r["id"], r["vendor"], r["amount"], r["invoice_date"], r["bank_idx"])
                for r in rows]

    # ----- flags -----
    def save_flags(self, org_id: str, anomalies: list[Anomaly]) -> None:
        with self._tx(org_id) as run:
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
            run("INSERT INTO reviews (org_id, invoice_id, verdict, action, note, rules, reviewer, reviewed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (org_id, invoice_id) DO UPDATE SET verdict = excluded.verdict, "
                "action = excluded.action, note = excluded.note, rules = excluded.rules, "
                "reviewer = excluded.reviewer, reviewed_at = excluded.reviewed_at",
                (org_id, invoice_id, verdict, action, (note or "").strip()[:500] or None,
                 ",".join(rules), reviewer, _now()))
        return action

    def review_details(self, org_id: str) -> dict[str, dict]:
        """{invoice id: {verdict, action, note, reviewer, reviewed_at}} for this company."""
        with self._tx(org_id) as run:
            rows = run("SELECT invoice_id, verdict, action, note, reviewer, reviewed_at FROM reviews "
                       "WHERE org_id = ?", (org_id,))
        return {r.pop("invoice_id"): r for r in rows}

    def all_invoices(self, org_id: str) -> list[dict]:
        """Every invoice of this company (no bank data), oldest first."""
        with self._tx(org_id) as run:
            return run("SELECT id, vendor, amount, invoice_date FROM invoices WHERE org_id = ? "
                       "ORDER BY invoice_date, id", (org_id,))

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
