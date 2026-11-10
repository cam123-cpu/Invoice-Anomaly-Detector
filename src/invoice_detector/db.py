"""SQLite storage. EVERY query is parameterized; identifiers go through an allow-list.

For production, move to PostgreSQL and apply postgres/rls_setup.sql (row-level security).
"""
import sqlite3
import threading

from .crypto import Vault
from .detection import Anomaly, Invoice
from .security import hash_password

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'accountant', 'auditor'))
);
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY,
  vendor TEXT NOT NULL,
  amount REAL NOT NULL CHECK (amount > 0),
  invoice_date TEXT NOT NULL,
  bank_enc TEXT,
  bank_idx TEXT,
  created_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anomalies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_id TEXT NOT NULL REFERENCES invoices(id),
  rule TEXT NOT NULL,
  severity TEXT NOT NULL,
  detail TEXT NOT NULL
);
"""

SORTABLE = {"id", "vendor", "amount", "invoice_date"}  # column names can't be parameters


def normalize_account(value: str) -> str:
    return "".join(value.split()).upper()


class Database:
    def __init__(self, path: str, vault: Vault):
        self.vault = vault
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # ----- users -----
    def create_user(self, username: str, password: str, role: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, hash_password(password), role))

    def get_user(self, username: str):
        with self._lock:
            return self.conn.execute(
                "SELECT username, password_hash, role FROM users WHERE username = ?",
                (username,)).fetchone()

    # ----- invoices -----
    def add_invoice(self, inv_id: str, vendor: str, amount: float, invoice_date: str,
                    bank_account: str | None, created_by: str) -> None:
        bank_enc = bank_idx = None
        if bank_account:
            bank_enc = self.vault.encrypt(bank_account, f"invoices.bank:{inv_id}")
            bank_idx = self.vault.blind_index(normalize_account(bank_account))
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO invoices (id, vendor, amount, invoice_date, bank_enc, bank_idx, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (inv_id, vendor, amount, invoice_date, bank_enc, bank_idx, created_by))

    def list_invoices(self, sort: str = "invoice_date", limit: int = 50) -> list[dict]:
        if sort not in SORTABLE:
            raise ValueError("Invalid sort column")
        limit = max(1, min(int(limit), 500))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT id, vendor, amount, invoice_date FROM invoices ORDER BY {sort} LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]  # bank details are never returned here

    def get_bank_account(self, inv_id: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT bank_enc FROM invoices WHERE id = ?",
                                    (inv_id,)).fetchone()
        if row is None:
            raise KeyError(inv_id)
        return self.vault.decrypt(row["bank_enc"], f"invoices.bank:{inv_id}") if row["bank_enc"] else None

    def detection_inputs(self) -> list[Invoice]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, vendor, amount, invoice_date, bank_idx FROM invoices").fetchall()
        return [Invoice(r["id"], r["vendor"], r["amount"], r["invoice_date"], r["bank_idx"])
                for r in rows]

    # ----- anomalies -----
    def save_flags(self, anomalies: list[Anomaly]) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM anomalies")
            self.conn.executemany(
                "INSERT INTO anomalies (invoice_id, rule, severity, detail) VALUES (?, ?, ?, ?)",
                [(a.invoice_id, a.rule, a.severity, a.detail) for a in anomalies])

    def list_anomalies(self, limit: int = 200) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            rows = self.conn.execute(
                "SELECT invoice_id, rule, severity, detail FROM anomalies "
                "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
                "invoice_id LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
