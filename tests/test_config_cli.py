import os
import tempfile
import unittest
from unittest import mock

from invoice_detector import cli
from invoice_detector.config import Settings, load_dotenv
from invoice_detector.db import Database


class DotenvTests(unittest.TestCase):
    def _load(self, data: bytes) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "wb") as f:
                f.write(data)
            with mock.patch.dict(os.environ, {"KEEP": "from-env"}, clear=True):
                load_dotenv(path)
                return dict(os.environ)

    def test_reads_values_and_real_env_wins(self):
        env = self._load(b"# comment\nA=1\nB='two'\nKEEP=from-file\n\nnot a pair\n")
        self.assertEqual((env["A"], env["B"], env["KEEP"]), ("1", "two", "from-env"))

    def test_reads_utf16_files_written_by_windows_powershell(self):
        self.assertEqual(self._load("A=1\r\nB=2\r\n".encode("utf-16"))["B"], "2")
        self.assertEqual(self._load(b"\xef\xbb\xbfA=1\n")["A"], "1")  # UTF-8 with BOM


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        old = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, old)

    def open_db(self):
        db = Database.from_settings(Settings.from_env())
        self.addCleanup(db.close)
        return db

    def test_init_org_seed_and_detect(self):
        self.assertEqual(cli.main(["init"]), 0)
        self.assertEqual(cli.main(["init"]), 1)  # never overwrites existing secrets
        with open(".env", encoding="utf-8") as f:
            self.assertIn("ENCRYPTION_KEYS=1:", f.read())
        self.assertEqual(cli.main(["seed", "Acme Inc"]), 1)  # company must exist first
        self.assertEqual(cli.main(["create-org", "Acme Inc"]), 0)
        self.assertEqual(cli.main(["create-org", "Acme Inc"]), 1)
        self.assertEqual(cli.main(["create-org", "Globex"]), 0)
        self.assertEqual(cli.main(["list-orgs"]), 0)
        self.assertEqual(cli.main(["migrate"]), 0)
        self.assertEqual(cli.main(["seed", "Acme Inc"]), 0)
        self.assertEqual(cli.main(["detect", "Acme Inc"]), 0)
        self.assertEqual(cli.main(["detect", "Nobody"]), 1)
        db = self.open_db()
        self.assertGreater(len(db.list_anomalies(db.org_id_by_name("Acme Inc"))), 10)
        self.assertEqual(db.list_invoices(db.org_id_by_name("Globex")), [])

    def test_create_user(self):
        cli.main(["init"])
        cli.main(["create-org", "Acme Inc"])
        with mock.patch("getpass.getpass", return_value="a-long-password"):
            self.assertEqual(cli.main(["create-user", "Acme Inc", "alice", "admin"]), 0)
            self.assertEqual(cli.main(["create-user", "Acme Inc", "alice", "admin"]), 1)  # taken
            self.assertEqual(cli.main(["create-user", "Acme Inc", "bob", "owner"]), 1)   # bad role
            self.assertEqual(cli.main(["create-user", "Nobody", "bob", "admin"]), 1)
        with mock.patch("getpass.getpass", return_value="short"):
            self.assertEqual(cli.main(["create-user", "Acme Inc", "bob", "admin"]), 1)
        self.assertEqual(self.open_db().auth_lookup("alice")["role"], "admin")

    def test_import_csv_skips_bad_rows(self):
        cli.main(["init"])
        cli.main(["create-org", "Acme Inc"])
        with open("in.csv", "w", encoding="utf-8") as f:
            f.write("id,vendor,amount,date,bank_account\n"
                    "A1,Acme,100.50,2026-01-05,ACCT1\n"
                    "A2,Acme,not-a-number,2026-01-06,ACCT1\n"
                    "A3,Acme,20,06/01/2026,\n"
                    "A1,Acme,100.50,2026-01-05,ACCT1\n")
        self.assertEqual(cli.main(["import-csv", "Acme Inc", "in.csv"]), 0)
        db = self.open_db()
        self.assertEqual([r["id"] for r in db.list_invoices(db.org_id_by_name("Acme Inc"))], ["A1"])

    def test_old_database_format_gets_a_clear_error(self):
        import sqlite3
        conn = sqlite3.connect("old.db")
        conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, vendor TEXT)")
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "older version"):
            Database("old.db", {1: b"k" * 32}, b"i" * 32)

    def test_unknown_command_prints_help(self):
        self.assertEqual(cli.main(["nope"]), 1)
        self.assertEqual(cli.main(["seed"]), 1)  # missing company


if __name__ == "__main__":
    unittest.main()
