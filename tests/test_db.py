import unittest

from invoice_detector.db import Conflict
from invoice_detector.detection import Anomaly
from support import DbTestCase


class DatabaseTests(DbTestCase):
    def add(self, inv_id="A1", vendor="Acme", amount=10.0, date="2026-01-05", bank=None, org=None):
        self.app.add_invoice(org or self.a, inv_id, vendor, amount, date, bank, "tester")

    def test_sql_injection_strings_are_stored_as_plain_data(self):
        evil = "x'); DROP TABLE invoices; --"
        self.add(vendor=evil)
        self.assertEqual(self.app.list_invoices(self.a)[0]["vendor"], evil)

    def test_injection_via_lookup_ids(self):
        self.add(bank="ACCT1")
        with self.assertRaises(KeyError):
            self.app.get_bank_account(self.a, "A1' OR '1'='1")
        self.assertIsNone(self.app.auth_lookup("nobody' OR '1'='1"))

    def test_credit_notes_are_stored_positive_and_shown_signed(self):
        self.add("B1", amount=100.0)
        self.app.add_invoice(self.a, "CN1", "Acme", 30.0, "2026-01-09", None, "tester", kind="credit")
        row = self.admin.conn.execute("SELECT amount, kind FROM invoices WHERE id = 'CN1'").fetchone()
        self.assertEqual((float(row["amount"]), row["kind"]), (30.0, "credit"))
        shown = {i["id"]: (i["amount"], i["kind"]) for i in self.app.list_invoices(self.a)}
        self.assertEqual(shown, {"B1": (100.0, "invoice"), "CN1": (-30.0, "credit")})
        self.assertEqual(self.app.get_invoices(self.a, ["CN1"])["CN1"]["amount"], -30.0)
        self.assertEqual(sum(i["amount"] for i in self.app.all_invoices(self.a)), 70.0)
        for_detection = {i.id: (i.amount, i.kind) for i in self.app.detection_inputs(self.a)}
        self.assertEqual(for_detection["CN1"], (30.0, "credit"))
        self.assertEqual(self.app.list_invoices(self.a, sort="amount")[0]["id"], "CN1")   # sorts as shown

    def test_sort_allow_list(self):
        for bad in ["amount; DROP TABLE invoices", "bank_enc", "org_id", "1", ""]:
            with self.assertRaises(ValueError):
                self.app.list_invoices(self.a, sort=bad)
        self.assertEqual(self.app.list_invoices(self.a, sort="amount"), [])

    def test_bank_account_encrypted_at_rest(self):
        self.add(bank="DE89 3704 0044 0532 0130 00")
        row = self.admin.conn.execute("SELECT bank_enc, bank_idx FROM invoices").fetchone()
        self.assertNotIn("DE89", f"{row['bank_enc']} {row['bank_idx']}")
        self.assertEqual(self.app.get_bank_account(self.a, "A1"), "DE89 3704 0044 0532 0130 00")
        self.assertNotIn("bank_enc", self.app.list_invoices(self.a)[0])

    def test_blind_index_ignores_spaces_and_case(self):
        self.add("A1", bank="de89 3704")
        self.add("A2", vendor="Globex", bank="DE893704")
        idx = {i.id: i.bank_idx for i in self.app.detection_inputs(self.a)}
        self.assertEqual(idx["A1"], idx["A2"])

    def test_constraints(self):
        with self.assertRaises(Conflict):
            self.add(amount=-5.0)
        self.add("A2")
        with self.assertRaises(Conflict):
            self.add("A2")
        with self.assertRaises(Conflict):
            self.app.create_user(self.a, "bob", "pw", "superuser")
        self.assertEqual(len(self.app.list_invoices(self.a)), 1)  # failed writes rolled back

    def test_users_store_hash_only(self):
        self.app.create_user(self.a, "alice", "a-long-password", "admin")
        user = self.app.auth_lookup("alice")
        self.assertNotIn("a-long-password", user["password_hash"])
        self.assertEqual((user["role"], user["org_id"]), ("admin", self.a))
        self.assertEqual(self.app.list_users(self.a),
                         [{"username": "alice", "role": "admin", "two_factor": False, "sso_only": False,
                           "disabled": False}])

    def test_reviews_snapshot_rules_and_feed_back(self):
        self.add("A1")
        self.add("A2", date="2026-01-06")
        self.app.save_flags(self.a, [Anomaly("A1", "duplicate", "high", "d"),
                                     Anomaly("A1", "round_amount", "low", "r"),
                                     Anomaly("A2", "round_amount", "low", "r")])
        self.app.set_review(self.a, "A1", "confirmed", "alice")
        self.app.set_review(self.a, "A2", "confirmed", "alice")
        self.app.set_review(self.a, "A2", "dismissed", "alice")  # changing your mind overwrites
        self.assertEqual(self.app.list_reviews(self.a), {"A1": "confirmed", "A2": "dismissed"})
        self.assertEqual(self.app.feedback_counts(self.a),
                         {"duplicate": (1, 0), "round_amount": (1, 1)})
        with self.assertRaises(KeyError):
            self.app.set_review(self.a, "not-flagged", "confirmed", "alice")
        with self.assertRaises(ValueError):
            self.app.set_review(self.a, "A1", "maybe", "alice")

    def test_settings_and_audit(self):
        self.assertEqual(self.app.get_org(self.a)["approval_threshold"], 10000)
        self.app.set_approval_threshold(self.a, 2500.0)
        self.assertEqual(self.app.get_org(self.a)["approval_threshold"], 2500.0)
        self.app.audit(self.a, "alice", "first")
        self.app.audit(self.a, "alice", "second", "detail")
        self.assertEqual([e["action"] for e in self.app.list_audit(self.a)], ["second", "first"])

    def test_review_actions_and_notes(self):
        for i, date in (("A1", "2026-01-05"), ("A2", "2026-01-06"), ("A3", "2026-01-07")):
            self.add(i, date=date)
        self.app.save_flags(self.a, [Anomaly("A1", "duplicate", "high", "d"),
                                     Anomaly("A2", "bank_account_change", "high", "b"),
                                     Anomaly("A3", "round_amount", "low", "r")])
        self.assertEqual(self.app.set_review(self.a, "A1", "confirmed", "alice"), "do_not_pay")
        self.assertEqual(self.app.set_review(self.a, "A2", "confirmed", "alice", note=" called vendor "), "hold")
        self.assertEqual(self.app.set_review(self.a, "A3", "dismissed", "alice", action="hold"), "pay")
        self.assertEqual(self.app.set_review(self.a, "A1", "confirmed", "bob", action="hold"), "hold")
        details = self.app.review_details(self.a)
        self.assertEqual((details["A1"]["action"], details["A1"]["reviewer"]), ("hold", "bob"))
        self.assertEqual((details["A2"]["action"], details["A2"]["note"]), ("hold", "called vendor"))
        self.assertEqual(details["A3"]["action"], "pay")
        with self.assertRaises(ValueError):
            self.app.set_review(self.a, "A1", "confirmed", "alice", action="shred")
        self.assertEqual(self.app.review_details(self.b), {})
        self.assertEqual([i["id"] for i in self.app.all_invoices(self.a)], ["A1", "A2", "A3"])

    def test_reset_password(self):
        from invoice_detector.security import verify_password
        self.app.create_user(self.a, "alice", "first-password-1", "admin")
        self.assertTrue(self.admin.set_password("alice", "second-password-2"))
        self.assertTrue(verify_password("second-password-2", self.app.auth_lookup("alice")["password_hash"]))
        self.assertFalse(self.admin.set_password("nobody", "whatever-password"))

    def test_two_factor_fields_and_sso_only_users(self):
        self.app.create_user(self.a, "alice", "a-long-password", "admin")
        self.app.create_user(self.a, "sso.user@acme.example", None, "auditor")
        self.assertEqual(self.app.auth_lookup("sso.user@acme.example")["password_hash"], "!sso")
        self.assertTrue(self.app.list_users(self.a)[1]["sso_only"])
        self.app.update_user_security(self.a, "alice", totp_secret="enc", totp_enabled=True,
                                      totp_last_step=5, recovery_codes=["h1", "h2"])
        sec = self.app.user_security(self.a, "alice")
        self.assertEqual((sec["totp_enabled"], sec["totp_last_step"], sec["recovery_codes"]), (True, 5, ["h1", "h2"]))
        self.assertTrue(self.app.auth_lookup("alice")["totp_enabled"])
        self.assertIsNone(self.app.user_security(self.b, "alice"))  # other company can't see it
        with self.assertRaises(ValueError):
            self.app.update_user_security(self.a, "alice", password_hash="x")  # not allow-listed
        self.app.update_user_security(self.b, "alice", totp_enabled=False)  # wrong company: no effect
        self.assertTrue(self.app.user_security(self.a, "alice")["totp_enabled"])
        self.assertFalse(self.app.set_password("alice", "new-password-123", org_id=self.b))
        self.assertTrue(self.app.set_password("alice", "new-password-123", org_id=self.a))

    def test_company_settings(self):
        self.assertFalse(self.app.get_org(self.a)["require_2fa"])
        self.app.set_org_settings(self.a, require_2fa=True)
        self.assertTrue(self.app.get_org(self.a)["require_2fa"])
        self.assertFalse(self.app.get_org(self.b)["require_2fa"])

    def test_key_check(self):
        import secrets as s
        from invoice_detector.db import Database, KeyMismatch
        self.app.check_keys()  # first run records the check value
        self.app.check_keys()  # later runs pass with the same keys
        if self.app.is_postgres:
            return
        wrong = Database.__new__(Database)
        wrong.__dict__.update(self.app.__dict__)
        wrong._keys, wrong._index_key, wrong._vaults = {1: s.token_bytes(32)}, s.token_bytes(32), {}
        with self.assertRaises(KeyMismatch):
            wrong.check_keys()

    def test_key_check_on_a_database_without_a_check_value(self):
        if self.app.is_postgres:
            self.skipTest("SQLite-specific")
        import secrets as s
        from invoice_detector.db import Database, KeyMismatch
        self.add(bank="ACCT-1")  # encrypted before any check value existed (older version)
        wrong = Database.__new__(Database)
        wrong.__dict__.update(self.app.__dict__)
        wrong._keys, wrong._index_key, wrong._vaults = {1: s.token_bytes(32)}, s.token_bytes(32), {}
        with self.assertRaises(KeyMismatch):
            wrong.check_keys()
        self.app.check_keys()  # the right keys pass and record the check value


class UpgradeTests(unittest.TestCase):
    def test_database_from_the_previous_version_is_upgraded(self):
        import os
        import re
        import secrets
        import sqlite3
        import tempfile

        from invoice_detector.db import Database
        from invoice_detector.schema import SQLITE_SCHEMA
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            old = sqlite3.connect(path)
            previous = SQLITE_SCHEMA.replace(
                "  action TEXT CHECK (action IN ('pay', 'hold', 'do_not_pay')),\n  note TEXT,\n", "")
            previous = re.sub(r"\n  (kind|reference) TEXT [^\n]*", "", previous)   # before credit notes
            old.executescript(previous)
            old.execute("INSERT INTO orgs (id, name) VALUES ('o1', 'Old Co')")
            old.execute("INSERT INTO invoices VALUES ('o1','I1','V',10,'2026-01-05',NULL,NULL,'x')")
            old.execute("INSERT INTO anomalies VALUES ('o1','I1','duplicate','high','d')")
            old.execute("INSERT INTO reviews (org_id, invoice_id, verdict, rules, reviewer, reviewed_at) "
                        "VALUES ('o1','I1','confirmed','duplicate','kim','2026-01-06')")
            old.commit()
            self.assertNotIn("action", [r[1] for r in old.execute("PRAGMA table_info(reviews)")])
            self.assertNotIn("kind", [r[1] for r in old.execute("PRAGMA table_info(invoices)")])
            old.close()
            db = Database(path, {1: secrets.token_bytes(32)}, secrets.token_bytes(32))
            try:
                self.assertEqual(db.review_details("o1")["I1"]["action"], None)  # old review kept
                self.assertEqual(db.get_invoices("o1", ["I1"])["I1"]["kind"], "invoice")  # old rows are invoices
                self.assertEqual(db.set_review("o1", "I1", "confirmed", "kim"), "do_not_pay")
            finally:
                db.close()  # before the folder is deleted: Windows can't delete an open file
            Database(path, {1: secrets.token_bytes(32)}, secrets.token_bytes(32)).close()  # idempotent


if __name__ == "__main__":
    unittest.main()
