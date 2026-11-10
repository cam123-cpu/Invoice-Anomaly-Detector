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
        with mock.patch("getpass.getpass", return_value="brand-new-password"):
            self.assertEqual(cli.main(["reset-password", "alice"]), 0)
            self.assertEqual(cli.main(["reset-password", "nobody"]), 1)
        with mock.patch("getpass.getpass", return_value="short"):
            self.assertEqual(cli.main(["reset-password", "alice"]), 1)
        from invoice_detector.security import verify_password
        self.assertTrue(verify_password("brand-new-password", self.open_db().auth_lookup("alice")["password_hash"]))

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
        self.assertEqual([r["id"] for r in db.list_invoices(db.org_id_by_name("Acme Inc"))],
                         ["A1", "A3"])  # A2 has a bad amount; the second A1 is a repeat
        self.assertEqual(cli.main(["import", "Acme Inc", "missing.xlsx"]), 1)
        self.assertEqual(cli.main(["import", "Nobody", "in.csv"]), 1)

    def test_old_database_format_gets_a_clear_error(self):
        import sqlite3
        conn = sqlite3.connect("old.db")
        conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, vendor TEXT)")
        conn.close()
        with self.assertRaisesRegex(RuntimeError, "older version"):
            Database("old.db", {1: b"k" * 32}, b"i" * 32)

    def test_init_deploy_writes_matching_secret_files_once(self):
        self.assertEqual(cli.main(["init-deploy", "secrets"]), 0)
        self.assertEqual(cli.main(["init-deploy", "secrets"]), 1)  # never overwrites keys
        def read(name):
            with open(os.path.join("secrets", name), encoding="utf-8") as f:
                return f.read().strip()
        self.assertIn(":" + read("app_db_password") + "@", read("database_url"))
        self.assertIn(":" + read("postgres_password") + "@", read("database_admin_url"))
        # The files work as *_FILE settings, exactly as docker-compose.prod.yml uses them.
        with mock.patch.dict(os.environ, {
                "SECRET_KEY_FILE": "secrets/secret_key",
                "ENCRYPTION_KEYS_FILE": "secrets/encryption_keys",
                "INDEX_KEY_FILE": "secrets/index_key"}):
            settings = Settings.from_env()
        self.assertEqual(settings.secret_key, read("secret_key"))

    def test_wrong_encryption_keys_are_refused_with_a_clear_message(self):
        cli.main(["init"])
        cli.main(["create-org", "Acme Inc"])
        cli.main(["seed", "Acme Inc"])
        os.rename(".env", "old.env")
        for name in ("SECRET_KEY", "ENCRYPTION_KEYS", "INDEX_KEY"):
            os.environ.pop(name, None)   # loaded from the old .env
        cli.main(["init"])   # new keys, same invoices.db
        with mock.patch("sys.stderr") as err:
            self.assertEqual(cli.run(["detect", "Acme Inc"]), 2)
        self.assertIn("key", "".join(c.args[0] for c in err.write.call_args_list).lower())

    def test_unknown_command_prints_help(self):
        self.assertEqual(cli.main(["nope"]), 1)
        self.assertEqual(cli.main(["seed"]), 1)  # missing company


if __name__ == "__main__":
    unittest.main()
