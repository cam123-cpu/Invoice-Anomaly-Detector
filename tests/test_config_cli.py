import os
import tempfile
import unittest
from unittest import mock

from invoice_detector import cli
from invoice_detector.config import Settings, load_dotenv
from invoice_detector.crypto import Vault
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
    def test_init_then_seed_and_detect(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            old = os.getcwd()
            os.chdir(tmp)
            try:
                self.assertEqual(cli.main(["init"]), 0)
                self.assertEqual(cli.main(["init"]), 1)  # never overwrites existing secrets
                with open(".env", encoding="utf-8") as f:
                    self.assertIn("ENCRYPTION_KEYS=1:", f.read())
                self.assertEqual(cli.main(["seed"]), 0)
                self.assertEqual(cli.main(["detect"]), 0)
                settings = Settings.from_env()
                db = Database(settings.db_path, Vault.from_settings(settings))
                self.assertGreater(len(db.list_anomalies()), 10)
                db.close()
            finally:
                os.chdir(old)

    def test_import_csv_skips_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            old = os.getcwd()
            os.chdir(tmp)
            try:
                cli.main(["init"])
                with open("in.csv", "w", encoding="utf-8") as f:
                    f.write("id,vendor,amount,date,bank_account\n"
                            "A1,Acme,100.50,2026-01-05,ACCT1\n"
                            "A2,Acme,not-a-number,2026-01-06,ACCT1\n"
                            "A3,Acme,20,06/01/2026,\n"
                            "A1,Acme,100.50,2026-01-05,ACCT1\n")
                self.assertEqual(cli.main(["import-csv", "in.csv"]), 0)
                settings = Settings.from_env()
                db = Database(settings.db_path, Vault.from_settings(settings))
                self.assertEqual([r["id"] for r in db.list_invoices()], ["A1"])
                db.close()
            finally:
                os.chdir(old)

    def test_unknown_command_prints_help(self):
        self.assertEqual(cli.main(["nope"]), 1)


if __name__ == "__main__":
    unittest.main()
