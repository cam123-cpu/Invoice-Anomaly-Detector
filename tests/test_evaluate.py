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

    def test_messy_ledger_is_not_the_synthetic_score(self):
        """Round retainers, Saturday bills and a weekly fee are ordinary, and the
        synthetic 98% figure never sees them. Precision here stays under the
        synthetic quality bar on purpose."""
        self.assertLess(self.result["precision"], 0.85)
        false = self.result["false_flags_by_rule"]
        self.assertIn("round_amount", false)
        self.assertIn("weekend_invoice", false)
        self.assertIn("near_duplicate", false)
        fired = self.result["fired"]
        self.assertIn("duplicate", fired["NS-2"])
        self.assertEqual(fired["AC-2"], ["duplicate"])
        self.assertEqual(fired["AC-3"], ["similar_vendor_name"])


if __name__ == "__main__":
    unittest.main()
