import pathlib
import unittest

from invoice_detector.detection import Invoice, detect
from invoice_detector.importer import convert, preview, read_table
from invoice_detector.samples import SAMPLES, build

REPO_SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "samples"


class SampleTests(unittest.TestCase):
    def load(self, name, data=None, sheet=None):
        table = read_table(name, data or build(name), sheet)
        info = preview(table)
        return table, info, convert(table, info["mapping"], info["date_order"])

    def test_each_sample_imports_with_the_suggested_mapping(self):
        expected = {"quickbooks-unpaid-bills.xlsx": ("Num", "MDY", 0),
                    "european-supplier-ledger.xlsx": ("Invoice No.", "DMY", 0),
                    "ap-register-2026.xlsx": ("Ref", "MDY", 1),
                    "invoice-template.xlsx": ("Invoice number", "MDY", 0)}
        for name, (id_col, order, errors) in expected.items():
            table, info, result = self.load(name)
            self.assertEqual((info["mapping"]["id"], info["date_order"], len(result.errors)),
                             (id_col, order, errors), name)
            self.assertFalse(info["date_order_ambiguous"], name)

    def test_planted_problems_are_found(self):
        _, _, result = self.load("european-supplier-ledger.xlsx")
        invoices = [Invoice(i["id"], i["vendor"], i["amount"], i["date"], i["bank_account"]) for i in result.invoices]
        rules = {a.rule for a in detect(invoices)}
        for rule in ("duplicate", "bank_account_change", "shared_bank_account", "split_invoice", "similar_vendor_name"):
            self.assertIn(rule, rules)

    def test_committed_copies_match_the_generator(self):
        """samples/ in the repository must be regenerated (scripts/make_samples.py) when samples.py changes."""
        for name in SAMPLES:
            path = REPO_SAMPLES / name
            self.assertTrue(path.exists(), f"missing samples/{name}")
            _, _, committed = self.load(name, path.read_bytes())
            _, _, fresh = self.load(name)
            self.assertEqual(committed.invoices, fresh.invoices, name)


if __name__ == "__main__":
    unittest.main()
