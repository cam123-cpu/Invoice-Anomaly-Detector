import unittest

from invoice_detector.evaluate import evaluate, format_report


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


if __name__ == "__main__":
    unittest.main()
