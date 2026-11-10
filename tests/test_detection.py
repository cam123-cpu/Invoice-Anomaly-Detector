import unittest

from invoice_detector.demo import generate_demo
from invoice_detector.detection import (Invoice, detect, find_amount_outliers, find_duplicates,
                                        find_ml_outliers, find_shared_bank_accounts, risk_scores)


def to_invoices(rows):
    # in the real app, bank_idx is a keyed hash; here the raw string is fine for matching
    return [Invoice(r["id"], r["vendor"], r["amount"], r["date"], r["bank_account"]) for r in rows]


class DetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.injected = generate_demo(seed=0)
        cls.invoices = to_invoices(rows)
        cls.found = detect(cls.invoices)
        cls.pairs = {(a.invoice_id, a.rule) for a in cls.found}

    def test_every_injected_anomaly_is_caught_by_its_rule(self):
        for rule, inv_id in self.injected.items():
            self.assertIn((inv_id, rule), self.pairs, f"{rule} on {inv_id} was missed")

    def test_ml_flags_the_extreme_outlier(self):
        flagged = {a.invoice_id for a in find_ml_outliers(self.invoices)}
        self.assertIn(self.injected["amount_outlier"], flagged)

    def test_few_false_positives_on_clean_data(self):
        injected_ids = set(self.injected.values())
        noisy = [a for a in find_amount_outliers(self.invoices) if a.invoice_id not in injected_ids]
        self.assertLessEqual(len(noisy), 2)
        self.assertEqual(len(find_duplicates(self.invoices)), 1)
        # shared bank account: only the minority vendor's invoice is flagged
        self.assertEqual([a.invoice_id for a in find_shared_bank_accounts(self.invoices)],
                         [self.injected["shared_bank_account"]])

    def test_deterministic(self):
        self.assertEqual(detect(self.invoices), detect(self.invoices))

    def test_results_sorted_high_first_and_risk_ranking(self):
        ranks = [a.severity for a in self.found]
        self.assertEqual(ranks, sorted(ranks, key=["high", "medium", "low"].index))
        top = next(iter(risk_scores(self.found)))
        self.assertIn(top, self.injected.values())

    def test_edge_cases(self):
        self.assertEqual(detect([]), [])
        one = [Invoice("A", "V", 10.0, "2026-01-05")]
        self.assertEqual(detect(one), [])  # tiny datasets don't crash or flag
        same = [Invoice(str(i), "V", 100.0, f"2026-01-{5 + i:02d}") for i in range(6)]
        self.assertEqual(find_amount_outliers(same), [])  # zero spread, no division by zero


if __name__ == "__main__":
    unittest.main()
