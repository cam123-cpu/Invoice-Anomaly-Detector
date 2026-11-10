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
        self.assertEqual(self.app.list_users(self.a), [{"username": "alice", "role": "admin"}])

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


if __name__ == "__main__":
    unittest.main()
