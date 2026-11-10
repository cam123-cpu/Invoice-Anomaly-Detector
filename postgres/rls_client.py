"""App-side companion to rls_setup.sql (psycopg 3): sets the user identity per
transaction and uses parameterized queries only.   pip install "psycopg[binary]"
"""
import os
from contextlib import contextmanager
from uuid import UUID

import psycopg
from psycopg import errors


def connect() -> psycopg.Connection:
    # Connect as app_user (NOT the owner/superuser). URL comes from the environment.
    return psycopg.connect(os.environ["DATABASE_URL"])


@contextmanager
def as_user(conn: psycopg.Connection, user_id: UUID):
    """user_id must come from a VERIFIED login token (e.g. secure_app.read_token),
    never from a request field the client controls."""
    with conn.transaction():
        # is_local=true: the setting dies when the transaction ends, so pooled
        # connections can't leak one user's identity into the next request.
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        yield conn


def list_invoices(conn, user_id: UUID, limit: int = 50):
    with as_user(conn, user_id):
        return conn.execute(
            "SELECT id, vendor, amount, invoice_date, flagged "
            "FROM invoices ORDER BY invoice_date DESC LIMIT %s", (limit,)).fetchall()
    # No WHERE org_id = ... needed: RLS filters rows in the database itself.


def add_invoice(conn, user_id: UUID, org_id: UUID, vendor, amount, date, bank_enc):
    try:
        with as_user(conn, user_id):
            return conn.execute(
                "INSERT INTO invoices (org_id, vendor, amount, invoice_date, bank_enc, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (org_id, vendor, amount, date, bank_enc, user_id)).fetchone()[0]
    except errors.InsufficientPrivilege:      # RLS WITH CHECK failed
        raise PermissionError("Not allowed to add invoices to this organization")


def flag_invoice(conn, user_id: UUID, invoice_id: UUID, flagged: bool = True):
    with as_user(conn, user_id):
        cur = conn.execute("UPDATE invoices SET flagged = %s WHERE id = %s",
                           (flagged, invoice_id))
        if cur.rowcount == 0:                 # RLS hides rows you can't edit
            raise PermissionError("Invoice not found or not editable")


def delete_invoice(conn, user_id: UUID, invoice_id: UUID):
    with as_user(conn, user_id):
        cur = conn.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        if cur.rowcount == 0:
            raise PermissionError("Invoice not found or you are not an admin")
