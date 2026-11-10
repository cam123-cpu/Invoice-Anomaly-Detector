import unittest

from invoice_detector.decisions import (DECISION_TEXT, RULE_FIX, fix_steps, payment_decision,
                                        suggested_action)
from invoice_detector.detection import RULE_INFO


class DecisionTests(unittest.TestCase):
    def test_every_rule_has_a_fix(self):
        self.assertEqual(set(RULE_FIX), set(RULE_INFO))
        self.assertTrue(all(text for _, text in RULE_FIX.values()))

    def test_suggested_action_takes_the_strictest(self):
        self.assertEqual(suggested_action(["duplicate"]), "do_not_pay")
        self.assertEqual(suggested_action(["round_amount", "duplicate"]), "do_not_pay")
        self.assertEqual(suggested_action(["bank_account_change", "weekend_invoice"]), "hold")
        self.assertEqual(suggested_action([]), "hold")
        self.assertEqual(suggested_action(["something_new"]), "hold")

    def test_fix_steps_are_deduplicated_and_ordered(self):
        steps = fix_steps(["weekend_invoice", "duplicate", "duplicate"])
        self.assertEqual(len(steps), 2)
        self.assertTrue(steps[0].startswith("Don't pay"))

    def test_payment_decision(self):
        self.assertEqual(payment_decision(None), "pay")
        self.assertEqual(payment_decision({"status": "open", "rules": ["duplicate"]}), "review")
        self.assertEqual(payment_decision({"status": "dismissed", "rules": ["duplicate"]}), "pay")
        self.assertEqual(payment_decision({"status": "confirmed", "rules": ["duplicate"]}), "do_not_pay")
        self.assertEqual(payment_decision({"status": "confirmed", "rules": ["duplicate"], "action": "hold"}), "hold")
        self.assertEqual(payment_decision({"status": "confirmed", "rules": ["x"], "action": "bogus"}), "hold")
        self.assertEqual(set(DECISION_TEXT), {"do_not_pay", "hold", "review", "pay"})


if __name__ == "__main__":
    unittest.main()
