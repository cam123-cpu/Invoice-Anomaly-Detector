import sqlite3
import unittest

from support import make_db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db()

    def test_sql_injection_strings_are_stored_as_plain_data(self):
        evil = "x'); DROP TABLE invoices; --"
        self.db.add_invoice("A1", evil, 10.0, "2026-01-05", None, "tester")
        rows = self.db.list_invoices()
        self.assertEqual(rows[0]["vendor"], evil)  # stored literally, table still there

    def test_injection_via_lookup_ids(self):
        self.db.add_invoice("A1", "Acme", 10.0, "2026-01-05", "ACCT1", "tester")
        with self.assertRaises(KeyError):
            self.db.get_bank_account("A1' OR '1'='1")

    def test_sort_allow_list(self):
        for bad in ["amount; DROP TABLE invoices", "bank_enc", "1", ""]:
            with self.assertRaises(ValueError):
                self.db.list_invoices(sort=bad)
        self.assertEqual(self.db.list_invoices(sort="amount"), [])

    def test_bank_account_encrypted_at_rest(self):
        self.db.add_invoice("A1", "Acme", 10.0, "2026-01-05", "DE89 3704 0044 0532 0130 00", "t")
        raw = self.db.conn.execute("SELECT bank_enc, bank_idx FROM invoices").fetchone()
        dump = " ".join(str(v) for v in raw)
        self.assertNotIn("DE89", dump)
        self.assertEqual(self.db.get_bank_account("A1"), "DE89 3704 0044 0532 0130 00")

    def test_listing_never_exposes_bank_data(self):
        self.db.add_invoice("A1", "Acme", 10.0, "2026-01-05", "ACCT1", "t")
        self.assertNotIn("bank_enc", self.db.list_invoices()[0])

    def test_blind_index_ignores_spaces_and_case(self):
        self.db.add_invoice("A1", "Acme", 10.0, "2026-01-05", "de89 3704", "t")
        self.db.add_invoice("A2", "Globex", 10.0, "2026-01-06", "DE893704", "t")
        idx = {i.id: i.bank_idx for i in self.db.detection_inputs()}
        self.assertEqual(idx["A1"], idx["A2"])

    def test_constraints(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_invoice("A1", "Acme", -5.0, "2026-01-05", None, "t")
        self.db.add_invoice("A2", "Acme", 5.0, "2026-01-05", None, "t")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_invoice("A2", "Acme", 5.0, "2026-01-05", None, "t")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_user("bob", "pw", "superuser")

    def test_users_store_hash_only(self):
        self.db.create_user("alice", "a-long-password", "admin")
        self.assertNotIn("a-long-password", self.db.get_user("alice")["password_hash"])
        self.assertIsNone(self.db.get_user("nobody' OR '1'='1"))


if __name__ == "__main__":
    unittest.main()
