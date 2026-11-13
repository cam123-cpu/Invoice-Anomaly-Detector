"""Proof that one company cannot read or change another company's data.

TenantIsolationTests go through the application's data layer and run on both SQLite
and PostgreSQL. PostgresRowLevelSecurityTests bypass the application entirely: they
send raw SQL with NO org filter as the app's database role, and check that
PostgreSQL itself refuses to cross the boundary.
"""
import os
import unittest
from contextlib import contextmanager

from invoice_detector.db import Conflict
from invoice_detector.detection import Anomaly, detect
from support import APP_URL, TABLES, USING_POSTGRES, DbTestCase


class TenantSeedMixin:
    def seed(self):
        """Both companies deliberately use the same invoice ids, vendor and bank account."""
        for org, who in ((self.a, "alice"), (self.b, "bob")):
            self.app.create_user(org, who, f"{who}-long-password", "admin")
            self.app.add_invoice(org, "INV-1", "Acme", 100.0, "2026-01-05", "SHARED-ACCT", who)
            self.app.audit(org, who, "seeded")
        self.app.add_invoice(self.b, "INV-2", "Globex", 999.0, "2026-01-06", "B-ONLY-ACCT", "bob")
        self.app.save_flags(self.a, [Anomaly("INV-1", "duplicate", "high", "a-flag")])
        self.app.save_flags(self.b, [Anomaly("INV-1", "duplicate", "high", "b-flag"),
                                     Anomaly("INV-2", "round_amount", "low", "b-flag")])
        self.app.set_review(self.a, "INV-1", "confirmed", "alice")
        self.app.set_review(self.b, "INV-2", "dismissed", "bob")
        for org, who in ((self.a, "alice"), (self.b, "bob")):  # both received the very same PDF
            self.app.add_document_invoices(org, [{"id": "DOC-1", "vendor": "Acme", "amount": 50.0,
                                                  "date": "2026-01-07", "sha256": "f" * 64, "filename": "a.pdf",
                                                  "method": "PDF text", "totals_issue": f"{who}-issue"}], who)
            # both taught the reader about the same vendor, differently
            self.app.learn_reading(org, "acme", "amount", "total" if who == "alice" else "balance due", True)
            self.app.mark_paid(org, ["INV-1"], who)


class TenantIsolationTests(TenantSeedMixin, DbTestCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_same_invoice_id_can_exist_in_both_companies(self):
        self.assertEqual([i["id"] for i in self.app.list_invoices(self.a)], ["INV-1", "DOC-1"])
        self.assertEqual([i["id"] for i in self.app.list_invoices(self.b)], ["INV-1", "INV-2", "DOC-1"])

    def test_reads_are_scoped_to_one_company(self):
        self.assertEqual({a["detail"] for a in self.app.list_anomalies(self.a)}, {"a-flag"})
        self.assertEqual(self.app.list_reviews(self.a), {"INV-1": "confirmed"})
        self.assertEqual(self.app.feedback_counts(self.a), {"duplicate": (1, 0)})
        self.assertEqual([u["username"] for u in self.app.list_users(self.a)], ["alice"])
        self.assertEqual({e["username"] for e in self.app.list_audit(self.a)}, {"alice"})
        self.assertEqual(len(self.app.detection_inputs(self.a)), 2)
        self.assertEqual({i.totals_issue for i in self.app.detection_inputs(self.a)}, {None, "alice-issue"})
        self.assertEqual(self.app.document_by_hash(self.a, "f" * 64)["uploaded_by"], "alice")
        self.assertEqual(self.app.document_stats(self.a)["documents"], 1)
        self.assertEqual(self.app.get_org(self.a)["name"], "Company A")

    def test_cannot_fetch_another_companys_invoice_by_id(self):
        with self.assertRaises(KeyError):
            self.app.get_bank_account(self.a, "INV-2")  # exists only in company B
        with self.assertRaises(KeyError):
            self.app.set_review(self.a, "INV-2", "confirmed", "alice")

    def test_writes_do_not_touch_the_other_company(self):
        self.app.save_flags(self.a, [])  # clears A's flags only
        self.assertEqual(self.app.list_anomalies(self.a), [])
        self.assertEqual(len(self.app.list_anomalies(self.b)), 2)
        self.app.set_approval_threshold(self.a, 1.0)
        self.assertEqual(self.app.get_org(self.b)["approval_threshold"], 10000)

    def test_usernames_are_unique_across_companies(self):
        with self.assertRaises(Conflict):
            self.app.create_user(self.b, "alice", "another-long-password", "admin")

    def test_bank_accounts_are_encrypted_with_per_company_keys(self):
        row = {r["org_id"]: r for r in self.admin.conn.execute(
            "SELECT org_id, bank_enc, bank_idx FROM invoices WHERE id = 'INV-1'").fetchall()}
        # same account number, but no way to tell from the stored data
        self.assertNotEqual(row[self.a]["bank_idx"], row[self.b]["bank_idx"])
        with self.assertRaises(ValueError):  # A's ciphertext is unreadable with B's keys
            self.app.vault(self.b).decrypt(row[self.a]["bank_enc"], f"invoices.bank:{self.a}:INV-1")
        self.assertEqual(self.app.get_bank_account(self.a, "INV-1"), "SHARED-ACCT")

    def test_what_the_reader_learned_stays_in_its_company(self):
        self.assertEqual(self.app.reading_hints(self.a), {"acme": {"amount": ["total"]}})
        self.assertEqual(self.app.reading_hints(self.b), {"acme": {"amount": ["balance due"]}})

    def test_detection_never_mixes_companies(self):
        # A and B each pay a different vendor into the same account. Within one company
        # that would be a shared-bank-account flag; across companies it must not be.
        self.app.add_invoice(self.a, "INV-9", "Initech", 50.0, "2026-01-07", "CROSS-ACCT", "alice")
        self.app.add_invoice(self.b, "INV-9", "Hooli", 60.0, "2026-01-07", "CROSS-ACCT", "bob")
        for org in (self.a, self.b):
            rules = {a.rule for a in detect(self.app.detection_inputs(org))}
            self.assertNotIn("shared_bank_account", rules)


class PostgresRequiredTest(unittest.TestCase):
    def test_ci_really_ran_against_postgres(self):
        """In CI's PostgreSQL job, fail loudly instead of silently skipping the RLS tests."""
        if os.environ.get("REQUIRE_POSTGRES"):
            self.assertTrue(USING_POSTGRES, "REQUIRE_POSTGRES is set but the database URLs are not")


@unittest.skipUnless(USING_POSTGRES, "needs PostgreSQL: set TEST_DATABASE_URL and TEST_DATABASE_ADMIN_URL")
class PostgresRowLevelSecurityTests(TenantSeedMixin, DbTestCase):
    def setUp(self):
        super().setUp()
        self.seed()
        import psycopg
        self.denied = psycopg.errors.InsufficientPrivilege
        self.raw = psycopg.connect(APP_URL, autocommit=True)  # the app role, no app code
        self.addCleanup(self.raw.close)

    @contextmanager
    def as_org(self, org_id):
        with self.raw.transaction():
            self.raw.execute("SELECT set_config('app.org_id', %s, true)", (org_id,))
            yield self.raw

    def count_as_owner(self, table, org_id):
        return self.admin.conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE org_id = %s", (org_id,)).fetchone()["n"]

    def test_app_connects_as_an_unprivileged_role(self):
        row = self.raw.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                               "WHERE rolname = current_user").fetchone()
        self.assertEqual(row, ("invoice_app", False, False))

    def test_with_no_company_set_every_table_looks_empty(self):
        self.assertEqual(self.count_as_owner("invoices", self.b), 3)  # the data is there
        for table in TABLES:
            n = self.raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            self.assertEqual(n, 0, table)

    def test_unfiltered_queries_return_only_the_current_company(self):
        for table in TABLES:
            column = "id" if table == "orgs" else "org_id"
            with self.as_org(self.a) as conn:
                seen = {r[0] for r in conn.execute(f"SELECT {column} FROM {table}").fetchall()}
            self.assertEqual(seen, {self.a}, table)

    def test_asking_for_another_company_by_id_returns_nothing(self):
        with self.as_org(self.a) as conn:
            rows = conn.execute("SELECT * FROM invoices WHERE org_id = %s", (self.b,)).fetchall()
        self.assertEqual(rows, [])

    def test_cannot_insert_rows_into_another_company(self):
        statements = [
            ("INSERT INTO invoices (org_id, id, vendor, amount, invoice_date, created_by) "
             "VALUES (%s, 'EVIL', 'x', 1, '2026-01-01', 'alice')"),
            "INSERT INTO anomalies (org_id, invoice_id, rule, severity, detail) "
            "VALUES (%s, 'INV-2', 'duplicate', 'high', 'planted')",
            "INSERT INTO users (username, org_id, password_hash, role) VALUES ('mole', %s, 'x', 'admin')",
            "INSERT INTO audit_log (org_id, at, username, action, detail) VALUES (%s, 'now', 'x', 'x', 'x')",
            "INSERT INTO documents (org_id, sha256, filename, method, invoice_id, uploaded_by, uploaded_at) "
            "VALUES (%s, 'x', 'x', 'x', 'INV-2', 'x', 'now')",
            "INSERT INTO reading_hints (org_id, vendor, field, label, hits) VALUES (%s, 'acme', 'amount', 'x', 99)",
            "INSERT INTO payments (org_id, invoice_id, paid_at, paid_by) VALUES (%s, 'INV-1', 'now', 'x')",
        ]
        for statement in statements:
            with self.assertRaises(self.denied, msg=statement):
                with self.as_org(self.a) as conn:
                    conn.execute(statement, (self.b,))
        self.assertEqual(self.count_as_owner("invoices", self.b), 3)
        self.assertEqual(self.count_as_owner("users", self.b), 1)

    def test_unfiltered_update_and_delete_only_touch_the_current_company(self):
        with self.as_org(self.a) as conn:
            updated = conn.execute("UPDATE reviews SET reviewer = 'changed'").rowcount
            deleted = conn.execute("DELETE FROM anomalies").rowcount
        self.assertEqual((updated, deleted), (1, 1))
        self.assertEqual(self.count_as_owner("anomalies", self.b), 2)
        reviewers = {r["reviewer"] for r in self.admin.conn.execute(
            "SELECT reviewer FROM reviews WHERE org_id = %s", (self.b,)).fetchall()}
        self.assertEqual(reviewers, {"bob"})

    def test_cannot_move_a_row_to_another_company(self):
        with self.assertRaises(self.denied):
            with self.as_org(self.a) as conn:
                conn.execute("UPDATE reviews SET org_id = %s", (self.b,))

    def test_app_role_cannot_escalate_or_tamper(self):
        statements = [
            "INSERT INTO orgs (id, name) VALUES ('x', 'Evil Corp')",   # no new companies
            "UPDATE orgs SET name = 'renamed'",                        # only the threshold is editable
            "UPDATE invoices SET amount = 1",                          # invoices are immutable
            "DELETE FROM invoices",
            "DELETE FROM audit_log",                                   # audit trail is append-only
            "UPDATE audit_log SET detail = 'rewritten'",
            "ALTER TABLE invoices DISABLE ROW LEVEL SECURITY",         # cannot switch RLS off
            "DROP POLICY tenant_isolation ON invoices",
            "SELECT password_hash FROM users",                         # no company set => no rows...
        ]
        for statement in statements[:-1]:
            with self.assertRaises(self.denied, msg=statement):
                with self.as_org(self.a) as conn:
                    conn.execute(statement)
        self.assertEqual(self.raw.execute(statements[-1]).fetchall(), [])
        self.assertEqual(self.count_as_owner("audit_log", self.a), 1)


if __name__ == "__main__":
    unittest.main()
