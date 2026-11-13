"""Payment controls: separation of duties, second approval, paid status."""
import io
import unittest

from openpyxl import load_workbook

from invoice_detector.controls import (ControlError, check_approver, check_reviewer, needs_second_approval,
                                       releases_payment)
from invoice_detector.decisions import payment_decision
from invoice_detector.detection import Invoice, credit_links, detect
from invoice_detector.excel_out import payment_file
from support import DbTestCase


class RulesTests(unittest.TestCase):
    def test_separation_of_duties(self):
        with self.assertRaises(ControlError):
            check_reviewer("alice", "alice", reviewers=2)
        check_reviewer("alice", "bob", reviewers=2)
        check_reviewer("alice", "alice", reviewers=1)       # a company of one can't separate anything
        check_reviewer(None, "alice", reviewers=2)          # added by an import with no user

    def test_what_needs_a_second_person(self):
        self.assertTrue(releases_payment("dismissed", "pay"))
        self.assertTrue(releases_payment("confirmed", "pay"))
        self.assertFalse(releases_payment("confirmed", "hold"))
        self.assertTrue(needs_second_approval(10_000, "invoice", 10_000, "dismissed", "pay", 2))
        self.assertFalse(needs_second_approval(9_999.99, "invoice", 10_000, "dismissed", "pay", 2))
        self.assertFalse(needs_second_approval(50_000, "invoice", 10_000, "confirmed", "do_not_pay", 2))
        self.assertFalse(needs_second_approval(50_000, "credit", 10_000, "dismissed", "pay", 2))
        self.assertFalse(needs_second_approval(50_000, "invoice", 10_000, "dismissed", "pay", 1))

    def test_the_second_person_is_really_someone_else(self):
        for approver in ("bob", "carol"):
            with self.assertRaises(ControlError):
                check_approver(approver, reviewer="bob", added_by="carol")
        check_approver("dave", reviewer="bob", added_by="carol")

    def test_decision_waits_for_the_second_person(self):
        item = {"status": "dismissed", "rules": ["amount_outlier"], "needs_approval": True}
        self.assertEqual(payment_decision(item), "review")
        self.assertEqual(payment_decision({**item, "approved_by": "dave"}), "pay")
        held = {"status": "confirmed", "action": "hold", "rules": ["amount_outlier"], "needs_approval": False}
        self.assertEqual(payment_decision(held), "hold")


def inv(i, amount=500.0, date="2026-01-05", vendor="V", kind="invoice", paid=None, reference=None):
    return Invoice(i, vendor, amount, date, None, kind=kind, paid=paid, reference=reference)


class PaidDetectionTests(unittest.TestCase):
    def details(self, invoices):
        return {(a.invoice_id, a.rule): (a.detail, a.severity) for a in detect(invoices)}

    def test_a_credit_for_a_paid_invoice_is_a_refund_due(self):
        found = self.details([inv("B1", paid="2026-01-10"), inv("CN1", kind="credit", date="2026-01-20")])
        self.assertIn("refund", found[("CN1", "matched_credit")][0])
        self.assertEqual(found[("CN1", "matched_credit")][1], "high")
        self.assertIn("paid on 2026-01-10", found[("B1", "credited_invoice")][0])

    def test_a_reissue_of_a_paid_invoice_is_high_risk(self):
        found = self.details([inv("B1", paid="2026-01-10"), inv("CN1", kind="credit", date="2026-01-20"),
                              inv("B2", date="2026-01-22")])
        detail, severity = found[("B2", "reissued_invoice")]
        self.assertEqual(severity, "high")
        self.assertIn("pays twice", detail)

    def test_duplicates_say_the_original_was_paid(self):
        found = self.details([inv("B1", paid="2026-01-06"), inv("B2")])
        self.assertIn("already paid on 2026-01-06", found[("B2", "duplicate")][0])


class PaymentFileTests(unittest.TestCase):
    def test_paid_invoices_leave_the_run_and_partial_credits_are_netted(self):
        invoices = [{"id": "A", "vendor": "V", "amount": 900.0, "invoice_date": "2026-01-05", "kind": "invoice"},
                    {"id": "CN", "vendor": "V", "amount": -100.0, "invoice_date": "2026-01-07", "kind": "credit"},
                    {"id": "OLD", "vendor": "V", "amount": 250.0, "invoice_date": "2025-12-01", "kind": "invoice",
                     "paid_at": "2025-12-20T10:00:00Z"}]
        links = credit_links([inv("A", 900.0), inv("CN", 100.0, kind="credit", reference="A")])
        wb = load_workbook(io.BytesIO(payment_file(invoices, {}, "Acme", links)))
        ready = wb["Ready to pay"]
        rows = {ready.cell(r, 1).value: ready.cell(r, 8).value for r in (2, 3)}
        self.assertEqual(rows, {"A": "Pay 800.00 after credit note CN (-100.00).", "CN": "Deduct from invoice A."})
        self.assertEqual(ready.cell(4, 5).value, 800.0)                    # net total, without the paid one
        self.assertEqual([c.value for c in wb["Already paid"][2]][:6],
                         ["OLD", "Invoice", "V", "2025-12-01", 250.0, "2025-12-20"])
        self.assertEqual(wb["Summary"].cell(9, 2).value, 2)

    def test_awaiting_approval_is_said_in_the_file(self):
        invoices = [{"id": "BIG", "vendor": "V", "amount": 25_000.0, "invoice_date": "2026-01-05"}]
        queue = {"BIG": {"status": "dismissed", "rules": ["amount_outlier"], "risk": 40, "needs_approval": True,
                         "awaiting_approval": True, "reviewer": "bob"}}
        wb = load_workbook(io.BytesIO(payment_file(invoices, queue, "Acme")))
        self.assertIn("needs a second person", wb["Review first"].cell(2, 8).value)


class StoredPaymentsTests(DbTestCase):
    def test_payments_are_recorded_once_and_can_be_undone(self):
        self.app.add_invoice(self.a, "I1", "Acme", 100.0, "2026-01-05", None, "alice")
        self.assertEqual(self.app.mark_paid(self.a, ["I1"], "bob", "batch 7"), ["I1"])
        self.assertEqual(self.app.mark_paid(self.a, ["I1"], "bob"), [])
        self.assertEqual(self.app.payments(self.a)["I1"]["reference"], "batch 7")
        self.assertTrue(self.app.list_invoices(self.a)[0]["paid_at"])
        self.assertEqual(self.app.detection_inputs(self.a)[0].paid, self.app.payments(self.a)["I1"]["paid_at"][:10])
        self.assertEqual(self.app.invoice_facts(self.a, "I1")["created_by"], "alice")
        self.assertTrue(self.app.unmark_paid(self.a, "I1"))
        self.assertFalse(self.app.unmark_paid(self.a, "I1"))
        self.assertIsNone(self.app.list_invoices(self.a)[0]["paid_at"])
        self.assertEqual(self.app.payments(self.b), {})                     # the other company sees nothing

    def test_a_new_review_clears_the_second_approval(self):
        from invoice_detector.detection import Anomaly
        self.app.add_invoice(self.a, "I1", "Acme", 50_000.0, "2026-01-05", None, "alice")
        self.app.save_flags(self.a, [Anomaly("I1", "amount_outlier", "medium", "x")])
        self.app.set_review(self.a, "I1", "dismissed", "bob")
        self.app.approve_review(self.a, "I1", "carol")
        self.assertEqual(self.app.review_details(self.a)["I1"]["approved_by"], "carol")
        self.app.set_review(self.a, "I1", "dismissed", "bob", note="changed my mind twice")
        self.assertIsNone(self.app.review_details(self.a)["I1"]["approved_by"])


if __name__ == "__main__":
    unittest.main()
