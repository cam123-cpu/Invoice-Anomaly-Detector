"""Shared test helpers.

Tests run on SQLite by default. Set TEST_DATABASE_ADMIN_URL (owner) and
TEST_DATABASE_URL (the limited invoice_app role) to run the same tests, plus the
row-level-security tests, against a real PostgreSQL server.
"""
import os
import secrets
import unittest

from invoice_detector.config import Settings
from invoice_detector.db import Database

KEYS = {1: secrets.token_bytes(32)}
INDEX_KEY = secrets.token_bytes(32)
ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL")
APP_URL = os.environ.get("TEST_DATABASE_URL")
USING_POSTGRES = bool(ADMIN_URL and APP_URL)
TABLES = ("orgs", "users", "invoices", "anomalies", "reviews", "audit_log", "documents", "reading_hints", "payments")  # per-company tables


def make_settings(api_key=None):
    return Settings(secret_key="test-secret-" + "x" * 40, encryption_keys=KEYS,
                    index_key=INDEX_KEY, db_path=":memory:", anthropic_api_key=api_key)


def make_dbs():
    """Returns (admin, app). On SQLite they are the same object; on PostgreSQL the
    admin is the table owner and app is the limited role the web server uses."""
    if USING_POSTGRES:
        admin = Database(ADMIN_URL, KEYS, INDEX_KEY)
        admin.migrate()
        admin.conn.execute(f"TRUNCATE {', '.join(TABLES + ('meta', 'rate_limits'))} RESTART IDENTITY")
        return admin, Database(APP_URL, KEYS, INDEX_KEY)
    db = Database(":memory:", KEYS, INDEX_KEY)
    return db, db


def forget_cookies(client):
    """Make a test client drop cookies after every response. Most API tests sign requests
    with an Authorization header, as programs do; the browser's cookie session has its own
    tests, which use an ordinary client."""
    original = client.send

    def send(request, **kwargs):
        response = original(request, **kwargs)
        client.cookies.clear()
        return response

    client.send = send
    return client


class DbTestCase(unittest.TestCase):
    """Gives each test a clean database with two companies, A and B."""

    def setUp(self):
        self.admin, self.app = make_dbs()
        self.addCleanup(self.admin.close)
        self.addCleanup(self.app.close)
        self.a = self.admin.create_org("Company A")
        self.b = self.admin.create_org("Company B")
