import unittest

from invoice_detector.demo import generate_demo
from invoice_detector.detection import (DEFAULT_CONFIG, DetectionConfig, Invoice, detect, find_amount_outliers,
                                        find_bank_account_changes, find_ml_outliers,
                                        find_near_duplicates, find_similar_vendor_names,
                                        find_split_invoices, learned_weights, match_credits, review_queue,
                                        risk_scores, RULE_WEIGHT)
from invoice_detector.evaluate import to_invoices


def inv(i, vendor="V", amount=100.0, date="2026-01-05", bank=None):
    return Invoice(str(i), vendor, amount, date, bank)


class DemoDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rows, cls.injected = generate_demo(seed=0)
        cls.invoices = to_invoices(rows)
        from dataclasses import replace

        from invoice_detector.demo import DEMO_END
        cls.found = detect(cls.invoices, replace(DEFAULT_CONFIG, today=DEMO_END))
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



def credit(i, vendor="V", amount=100.0, date="2026-01-20"):
    return Invoice(str(i), vendor, amount, date, None, kind="credit")


class CreditNoteTests(unittest.TestCase):
    def rules(self, invoices):
        fired = {}
        for a in detect(invoices):
            fired.setdefault(a.invoice_id, set()).add(a.rule)
        return fired

    def history(self, vendor="V", n=8):
        """Ordinary invoices, so the vendor has a history and nothing else fires."""
        from datetime import date, timedelta
        days = [date(2025, k + 1, 10) for k in range(n)]
        days = [d + timedelta(days=2) if d.weekday() >= 5 else d for d in days]   # keep them on weekdays
        return [inv(f"H{k}", vendor, 400.0 + 37 * k, d.isoformat()) for k, d in enumerate(days)]

    def test_a_cancelled_invoice_and_its_credit_are_flagged_as_a_pair(self):
        invoices = self.history() + [inv("B1", amount=912.40, date="2026-01-12"),
                                     credit("CN1", amount=912.40, date="2026-01-15")]
        self.assertEqual(match_credits(invoices), {"CN1": "B1"})
        fired = self.rules(invoices)
        self.assertEqual(fired["B1"], {"credited_invoice"})
        self.assertEqual(fired["CN1"], {"matched_credit"})
        details = {a.invoice_id: a.detail for a in detect(invoices)}
        self.assertIn("CN1", details["B1"])
        self.assertIn("B1", details["CN1"])

    def test_the_reissued_invoice_is_not_called_a_duplicate(self):
        invoices = self.history() + [inv("B1", amount=912.40, date="2026-01-12"),
                                     credit("CN1", amount=912.40, date="2026-01-15"),
                                     inv("B2", amount=912.40, date="2026-01-16")]   # the correct re-issue
        fired = self.rules(invoices)
        self.assertEqual(fired["B2"], {"reissued_invoice"})        # not a duplicate; waits for the credit
        self.assertEqual(fired["B1"], {"credited_invoice"})
        self.assertLess(RULE_WEIGHT["reissued_invoice"], RULE_WEIGHT["duplicate"])
        # ...but without the credit note, the same two invoices are a duplicate.
        without = self.rules([i for i in invoices if i.id != "CN1"])
        self.assertTrue({"duplicate", "near_duplicate"} & (without.get("B1", set()) | without.get("B2", set())))

    def test_each_invoice_is_cancelled_once_and_the_latest_one_is_taken(self):
        invoices = [inv("B1", amount=50, date="2026-01-02"), inv("B2", amount=50, date="2026-02-02"),
                    credit("CN1", amount=50, date="2026-02-10"), credit("CN2", amount=50, date="2026-02-11")]
        self.assertEqual(match_credits(invoices), {"CN1": "B2", "CN2": "B1"})

    def test_a_credit_never_cancels_a_later_or_much_older_invoice(self):
        self.assertEqual(match_credits([credit("CN1", amount=50, date="2026-01-01"),
                                        inv("B1", amount=50, date="2026-01-05")]), {})
        self.assertEqual(match_credits([inv("B1", amount=50, date="2024-01-05"),
                                        credit("CN1", amount=50, date="2026-01-01")]), {})
        self.assertEqual(match_credits([inv("B1", "Other", 50, "2026-01-05"),
                                        credit("CN1", amount=50, date="2026-01-09")]), {})

    def test_a_partial_credit_is_ordinary(self):
        invoices = self.history() + [inv("B1", amount=912.40, date="2026-01-12"),
                                     credit("CN1", amount=38.20, date="2026-01-15")]
        self.assertEqual(self.rules(invoices), {})

    def test_a_credit_from_nowhere_is_flagged(self):
        fired = self.rules(self.history() + [credit("CN1", "Soylent Partners", 4200.0)])
        self.assertEqual(fired["CN1"], {"unmatched_credit"})
        too_big = self.rules([inv("B1", amount=100, date="2026-01-02"), credit("CN1", amount=5000.0)])
        self.assertEqual(too_big["CN1"], {"unmatched_credit"})

    def test_the_same_credit_twice(self):
        invoices = self.history() + [credit("CN1", amount=38.20), credit("CN2", amount=38.20)]
        self.assertEqual(self.rules(invoices), {"CN2": {"duplicate_credit"}})

    def test_credits_do_not_teach_the_amount_checks(self):
        """A large credit must not look like a large purchase, nor shift what is normal."""
        base = self.history(n=10)
        with_credit = base + [credit("CN1", amount=399.0, date="2026-01-15")]
        self.assertEqual(self.rules(with_credit), self.rules(base))
        for a in detect(with_credit):
            self.assertNotEqual(a.invoice_id, "CN1")

    def test_a_credit_that_names_its_invoice(self):
        from invoice_detector.detection import credit_links
        base = self.history() + [inv("INV-77", amount=900.0, date="2026-01-05"),
                                 inv("INV-78", amount=100.0, date="2026-01-06")]
        ref = lambda i, amount, r, vendor="V": Invoice(i, vendor, amount, "2026-01-20", None, kind="credit", reference=r)  # noqa: E731
        # partial, named: reduces INV-77, no flag (and not matched to INV-78 by amount)
        partial = base + [ref("CN1", 100.0, "INV-77")]
        self.assertEqual(credit_links(partial), {"CN1": ("INV-77", "partial")})
        self.assertEqual(match_credits(partial), {})
        self.assertEqual(self.rules(partial), {})
        # full, named (formatting differences in the number don't matter)
        full = base + [ref("CN1", 900.0, "inv 77")]
        self.assertEqual(match_credits(full), {"CN1": "INV-77"})
        # names an invoice that doesn't exist, or another vendor's
        self.assertEqual(self.rules(base + [ref("CN1", 50.0, "INV-404")])["CN1"], {"unmatched_credit"})
        other = base + [inv("X-1", "Other", 300.0, "2026-01-07"), ref("CN1", 50.0, "X-1")]
        self.assertEqual(self.rules(other)["CN1"], {"unmatched_credit"})
        # credits for one invoice that add up to more than it
        over = base + [ref("CN1", 600.0, "INV-77"), ref("CN2", 400.0, "INV-77")]
        fired = self.rules(over)
        self.assertNotIn("CN1", fired)
        self.assertEqual(fired["CN2"], {"unmatched_credit"})
        self.assertIn("more than the invoice", next(a.detail for a in detect(over) if a.invoice_id == "CN2"))

    def test_a_fake_credit_cannot_launder_a_duplicate(self):
        """Attack: an invoice already paid is copied, and a credit note "cancels" the original
        so the copy no longer looks like a duplicate. The copy must not be ready to pay."""
        from invoice_detector.decisions import suggested_action
        invoices = self.history() + [inv("PAID-1", amount=548.0, date="2026-01-05"),
                                     inv("COPY-1", amount=548.0, date="2026-02-20"),
                                     credit("CN-FAKE", amount=548.0, date="2026-02-19")]
        fired = self.rules(invoices)
        self.assertEqual(fired["COPY-1"], {"reissued_invoice"})
        self.assertEqual(suggested_action(["reissued_invoice"]), "hold")
        detail = next(a.detail for a in detect(invoices) if a.invoice_id == "COPY-1")
        self.assertIn("CN-FAKE", detail)
        # each cancelled invoice has at most one replacement; a third copy is an ordinary duplicate
        invoices.append(inv("COPY-2", amount=548.0, date="2026-02-23"))
        self.assertIn("near_duplicate", self.rules(invoices)["COPY-2"])

    def test_every_credit_rule_has_a_weight_a_description_and_an_action(self):
        from invoice_detector.decisions import suggested_action
        from invoice_detector.detection import RULE_INFO
        for rule in ("credited_invoice", "matched_credit", "unmatched_credit", "duplicate_credit", "reissued_invoice"):
            self.assertIn(rule, RULE_WEIGHT)
            self.assertIn(rule, RULE_INFO)
            self.assertIn(suggested_action([rule]), ("hold", "do_not_pay"))


if __name__ == "__main__":
    unittest.main()


class NumberingAndDateTests(unittest.TestCase):
    def rules(self, invoices, **cfg):
        from dataclasses import replace
        config = replace(DEFAULT_CONFIG, use_ml=False, today="2026-06-30", **cfg)
        out = {}
        for a in detect(invoices, config):
            out.setdefault(a.invoice_id, set()).add(a.rule)
        return out

    def test_the_same_number_written_differently(self):
        from invoice_detector.detection import _number_key
        self.assertEqual(_number_key("INV-0042")[0], _number_key("inv 42")[0])
        self.assertEqual(_number_key("INV42")[0], _number_key("42")[0])
        self.assertNotEqual(_number_key("1001-A")[0], _number_key("1001")[0])
        found = self.rules([inv("INV-1001", "V", 500.0, "2026-03-02"), inv("INV1001", "V", 512.5, "2026-03-23"),
                            inv("INV 1001", "Other", 500.0, "2026-03-24"), inv("7", "V", 10.0, "2026-03-25"),
                            inv("A7", "V", 11.0, "2026-03-26")])
        self.assertEqual(found.get("INV1001"), {"duplicate_number"})
        self.assertNotIn("INV 1001", found)                          # another vendor's number
        self.assertNotIn("A7", found)                                # too short to mean anything

    def test_numbers_with_no_gaps(self):
        months = ["2026-01-05", "2026-02-04", "2026-03-04", "2026-04-06", "2026-05-06"]
        captive = [inv(f"PA-{2041 + k}", "Pinnacle", 1180.0 + 37 * k, d) for k, d in enumerate(months)]
        self.assertEqual(self.rules(captive)["PA-2045"], {"sequential_numbers"})
        normal = [inv(f"PA-{2041 + 7 * k}", "Pinnacle", 1180.0 + 37 * k, d) for k, d in enumerate(months)]
        self.assertNotIn("PA-2069", self.rules(normal))
        self.assertEqual(self.rules(captive[:3]), {})                # too few to tell

    def test_the_companys_own_numbering_is_ignored(self):
        """INV-0001, INV-0002... given by the company's system across all vendors: a vendor's
        invoices entered together are consecutive, which says nothing about the vendor."""
        rows, n = [], 0
        for vendor in ("A", "B", "C", "D"):
            for k in range(8):
                n += 1
                rows.append(inv(f"INV-{n:04d}", vendor, 300.0 + 11 * k, f"2026-0{1 + k % 6}-0{2 + k % 3}"))
        self.assertFalse(any("sequential_numbers" in r for r in self.rules(rows).values()))

    def test_future_dates(self):
        found = self.rules([inv("F1", "V", 100.0, "2026-07-20"), inv("F2", "V", 101.0, "2026-07-03")])
        self.assertEqual(found.get("F1"), {"future_date"})
        self.assertNotIn("F2", found)                                # within the 7-day allowance
