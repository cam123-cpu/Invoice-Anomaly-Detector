import unittest

from invoice_detector.evaluate import evaluate, evaluate_adversarial, format_report


class EvaluateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = evaluate(seeds=range(3))

    def test_quality_bar_on_synthetic_data(self):
        """Guards against regressions when rules or thresholds change."""
        self.assertGreaterEqual(self.result["precision"], 0.85)
        self.assertGreaterEqual(self.result["recall"], 0.80)
        self.assertGreaterEqual(self.result["top_k_hit_rate"], 0.85)

    def test_every_rule_is_exercised_and_fires(self):
        self.assertGreaterEqual(len(self.result["rule_recall"]), 11)
        for rule, recall in self.result["rule_recall"].items():
            self.assertGreaterEqual(recall, 0.9, rule)

    def test_report_is_readable(self):
        text = format_report(self.result)
        self.assertIn("Precision", text)
        self.assertIn("duplicate", text)


class AdversarialEvaluateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = evaluate_adversarial()

    def test_routine_is_quiet_and_every_trap_is_still_caught(self):
        """Round retainers, Saturday bills and a weekly fee are how those vendors work, so
        they are not flagged. Each hides a real problem too (a duplicate, an off-schedule
        copy, a bank change), and those must still be caught: quieting a vendor's routine
        must never hide what breaks it. This ledger is scored separately from the
        synthetic benchmark; neither number stands in for the other."""
        fired, false = self.result["fired"], self.result["false_flags_by_rule"]
        for rule in ("round_amount", "weekend_invoice", "near_duplicate"):
            self.assertNotIn(rule, false)
        self.assertEqual(self.result["recall"], 1.0)
        self.assertIn("near_duplicate", fired["RT-EXTRA"])
        self.assertIn("duplicate", fired["RC-DUP"])
        self.assertIn("bank_account_change", fired["SC-BANK"])
        self.assertIn("duplicate", fired["NS-2"])
        self.assertEqual(fired["AC-2"], ["duplicate"])
        self.assertEqual(fired["AC-3"], ["similar_vendor_name"])
        self.assertGreaterEqual(self.result["precision"], 0.8)

if __name__ == "__main__":
    unittest.main()
