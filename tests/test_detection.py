import unittest

from invoice_detector.demo import generate_demo
from invoice_detector.detection import (DetectionConfig, Invoice, detect, find_amount_outliers,
                                        find_bank_account_changes, find_ml_outliers,
                                        find_near_duplicates, find_similar_vendor_names,
                                        find_split_invoices, learned_weights, review_queue,
                                        risk_scores, RULE_WEIGHT)
from invoice_detector.evaluate import to_invoices


def inv(i, vendor="V", amount=100.0, date="2026-01-05", bank=None):
    return Invoice(str(i), vendor, amount, date, bank)


class DemoDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.injected = generate_demo(seed=0)
        cls.invoices = to_invoices(rows)
        cls.found = detect(cls.invoices)
        cls.fired = {}
        for a in cls.found:
            cls.fired.setdefault(a.invoice_id, set()).add(a.rule)

    def test_every_injected_anomaly_is_caught_by_its_rule(self):
        for inv_id, rules in self.injected.items():
            self.assertLessEqual(rules, self.fired.get(inv_id, set()), f"missed on {inv_id}")

    def test_few_false_flags_on_clean_invoices(self):
        false_flags = [a for a in self.found if a.invoice_id not in self.injected]
        self.assertLessEqual(len(false_flags), 3)

    def test_ml_flags_are_mostly_real(self):
        flagged = {a.invoice_id for a in find_ml_outliers(self.invoices)}
        self.assertTrue(flagged)
        self.assertGreaterEqual(len(flagged & self.injected.keys()) / len(flagged), 0.8)

    def test_deterministic(self):
        self.assertEqual(detect(self.invoices), detect(self.invoices))

    def test_review_queue_ranks_real_anomalies_first(self):
        queue = review_queue(self.found)
        risks = [q["risk"] for q in queue]
        self.assertEqual(risks, sorted(risks, reverse=True))
        self.assertTrue(all(q["invoice_id"] in self.injected for q in queue[:10]))
        self.assertTrue(all(0 <= r <= 100 for r in risks))


class RuleTests(unittest.TestCase):
    def test_edge_cases_do_not_crash_or_flag(self):
        self.assertEqual(detect([]), [])
        self.assertEqual(detect([inv(1)]), [])
        same = [inv(i, date=f"2026-01-{5 + i:02d}") for i in range(0, 20, 2)]
        self.assertEqual(find_amount_outliers(same), [])  # zero spread: no division by zero

    def test_outlier_cannot_hide_by_inflating_the_baseline(self):
        history = [inv(i, amount=100.0 + i, date=f"2026-02-{i + 1:02d}") for i in range(10)]
        flagged = find_amount_outliers(history + [inv("big", amount=50_000.0, date="2026-03-02")])
        self.assertEqual([(a.invoice_id, a.severity) for a in flagged], [("big", "high")])

    def test_near_duplicate_ignores_monthly_subscriptions(self):
        monthly = [inv(i, amount=2400.0, date=f"2026-{m:02d}-05") for i, m in enumerate(range(1, 7))]
        self.assertEqual(find_near_duplicates(monthly), [])
        resubmitted = monthly + [inv("again", amount=2400.0, date="2026-03-09")]
        self.assertEqual([a.invoice_id for a in find_near_duplicates(resubmitted)], ["again"])

    def test_bank_account_change_needs_history_and_flags_first_use_only(self):
        usual = [inv(i, date=f"2026-01-{i + 5:02d}", bank="A") for i in range(4)]
        new = [inv("n1", date="2026-02-01", bank="B"), inv("n2", date="2026-02-08", bank="B")]
        self.assertEqual([a.invoice_id for a in find_bank_account_changes(usual + new)], ["n1"])
        self.assertEqual(find_bank_account_changes(usual[:2] + new), [])  # too little history

    def test_split_invoices_respect_window_and_threshold(self):
        split = [inv(1, amount=4000.0, date="2026-03-02"), inv(2, amount=3500.0, date="2026-03-03"),
                 inv(3, amount=3000.0, date="2026-03-04")]
        self.assertEqual({a.invoice_id for a in find_split_invoices(split)}, {"1", "2", "3"})
        spread_out = [inv(1, amount=6000.0, date="2026-03-02"), inv(2, amount=6000.0, date="2026-03-20")]
        self.assertEqual(find_split_invoices(spread_out), [])
        cfg = DetectionConfig(approval_threshold=50_000.0)
        self.assertEqual(find_split_invoices(split, cfg), [])

    def test_similar_vendor_names_flag_the_smaller_vendor(self):
        data = [inv(i, vendor="Acme Supplies", date=f"2026-01-{i + 5:02d}") for i in range(5)]
        data += [inv("x", vendor="Acme Suppiles"), inv("y", vendor="Globex Corp")]
        self.assertEqual([a.invoice_id for a in find_similar_vendor_names(data)], ["x"])

    def test_risk_combines_signals_but_stays_bounded(self):
        found = detect([inv(1, amount=5000.0, date="2026-01-03"), inv(2, amount=5000.0, date="2026-01-03")])
        risks = risk_scores(found)
        self.assertGreater(risks["2"], risks["1"])  # the duplicate outranks the original
        self.assertLessEqual(max(risks.values()), 100)

    def test_feedback_tunes_rule_weights(self):
        self.assertEqual(learned_weights({}), RULE_WEIGHT)  # no reviews: nothing changes
        noisy = learned_weights({"weekend_invoice": (0, 40), "duplicate": (30, 0), "made_up": (1, 1)})
        self.assertLessEqual(noisy["weekend_invoice"], RULE_WEIGHT["weekend_invoice"] / 2)
        self.assertGreaterEqual(noisy["weekend_invoice"], 0.05)  # never silenced completely
        self.assertEqual(noisy["duplicate"], RULE_WEIGHT["duplicate"])  # confirmations keep it
        self.assertNotIn("made_up", noisy)
        few = learned_weights({"near_duplicate": (0, 1)})  # one dismissal barely moves it
        self.assertGreater(few["near_duplicate"], RULE_WEIGHT["near_duplicate"] * 0.8)

    def test_learned_weights_reorder_the_queue(self):
        found = detect([inv(1, amount=5000.0, date="2026-01-05"), inv(2, amount=5000.0, date="2026-01-05"),
                        inv(3, vendor="W", amount=7000.0, date="2026-01-07")])
        self.assertEqual(review_queue(found)[0]["invoice_id"], "2")
        tuned = learned_weights({"duplicate": (0, 500), "round_amount": (0, 500)})
        self.assertLess(risk_scores(found, tuned)["2"], risk_scores(found)["2"])


if __name__ == "__main__":
    unittest.main()
