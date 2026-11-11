from dataclasses import replace

from doc_templates import TEMPLATES
from invoice_detector import doc_samples
from invoice_detector.detection import detect
from invoice_detector.intake import DocumentIntake, IntakeError
from invoice_detector.security import TokenSigner
from support import DbTestCase, make_settings


def answer(**fields):
    base = {"invoice_number": "OCR-1", "vendor_name": "Stark Freight", "invoice_date": "2026-10-30",
            "total_amount": 480.0}
    base.update(fields)
    return {"content": [{"type": "tool_use", "name": "record_invoice", "input": base}]}


class IntakeTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.settings = make_settings()
        self.signer = TokenSigner(self.settings.secret_key)
        self.intake = DocumentIntake(self.app, self.signer, self.settings, isolate=False)
        self.user = {"sub": "alice", "org": self.a, "role": "admin"}
        self.samples = {name: (data, doc) for name, data, doc in doc_samples.sample_docs()}

    @staticmethod
    def checked(result, **changes):
        """What the browser sends back after a person checked a draft."""
        return {"draft": result["draft"], "file": result["file"], **result["fields"], **changes}


class ExtractTests(IntakeTestCase):
    def test_draft_then_import(self):
        data, doc = self.samples["stark-freight-8811.pdf"]
        draft = self.intake.extract(self.user, "stark-freight-8811.pdf", data)
        self.assertEqual(draft["status"], "ready")
        self.assertEqual(draft["fields"]["id"], "SF-8811")
        self.assertEqual(self.app.list_invoices(self.a), [])   # nothing saved by reading
        result = self.intake.import_drafts(self.user, [self.checked(draft)])
        self.assertEqual((result["added"], result["fields_corrected"]), (1, 0))
        self.assertEqual(self.app.get_bank_account(self.a, "SF-8811"), "GB29NWBK60161331926819")
        self.assertIn("1 invoice from documents", str(self.app.list_audit(self.a)))

    def test_the_same_file_is_never_imported_twice(self):
        data, _ = self.samples["wayne-office-1042.pdf"]
        draft = self.intake.extract(self.user, "a.pdf", data)
        self.intake.import_drafts(self.user, [self.checked(draft)])
        again = self.intake.extract(self.user, "renamed.pdf", data)
        self.assertEqual((again["status"], again["invoice_id"]), ("already_imported", "WO-1042"))
        stale = self.intake.import_drafts(self.user, [self.checked(draft)])   # an old draft of it, resent
        self.assertEqual(stale["added"], 0)
        self.assertIn("already imported", stale["skipped"][0]["reason"])

    def test_known_invoice_number_is_warned_about_and_skipped(self):
        self.app.add_invoice(self.a, "WO-1042", "Wayne Office", 10.0, "2026-01-05", None, "seed")
        data, _ = self.samples["wayne-office-1042.pdf"]
        draft = self.intake.extract(self.user, "a.pdf", data)
        self.assertIn("already on file", draft["warnings"][0])
        result = self.intake.import_drafts(self.user, [self.checked(draft)])
        self.assertEqual(result["added"], 0)

    def test_corrections_are_recorded_and_the_totals_issue_follows_the_final_amount(self):
        data, doc = self.samples["umbrella-ltd-3307.xlsx"]
        draft = self.intake.extract(self.user, "u.xlsx", data)
        self.assertTrue(any("add up" in w for w in draft["warnings"]))
        fixed = self.intake.import_drafts(self.user, [self.checked(draft, amount=doc.true_total, id="3307-B")])
        self.assertEqual(fixed["fields_corrected"], 2)
        self.assertEqual(fixed["totals_issues"], 0)   # the person typed the correct total
        stats = self.app.document_stats(self.a)
        self.assertEqual((stats["documents"], stats["fields_corrected"]), (1, 2))

    def test_totals_problem_becomes_a_detection_flag(self):
        data, _ = self.samples["umbrella-ltd-3307.xlsx"]
        draft = self.intake.extract(self.user, "u.xlsx", data)
        self.intake.import_drafts(self.user, [self.checked(draft)])
        flags = [a for a in detect(self.app.detection_inputs(self.a)) if a.rule == "totals_mismatch"]
        self.assertEqual([a.invoice_id for a in flags], ["3307"])
        self.assertIn("don't add up", flags[0].detail)

    def test_vendor_spelling_matches_the_existing_vendor(self):
        self.app.add_invoice(self.a, "OLD-1", "HLP Manufacturing Inc", 10.0, "2026-01-05", None, "seed")
        data, _ = TEMPLATES["Simple business invoice"]()
        draft = self.intake.extract(self.user, "hlp.xlsx", data)
        self.assertEqual(draft["fields"]["vendor"], "HLP Manufacturing Inc")

    def test_a_near_miss_spelling_is_never_merged(self):
        """"Acme Suplies" must stay as printed: merging it would hide a look-alike vendor."""
        self.app.add_invoice(self.a, "OLD-1", "Acme Supplies", 10.0, "2026-01-05", None, "seed")
        data, _ = self.samples["acme-suplies-0091.pdf"]
        draft = self.intake.extract(self.user, "a.pdf", data)
        self.assertEqual(draft["fields"]["vendor"], "Acme Suplies")
        self.assertIn("look-alike", " ".join(draft["warnings"]))

    def test_unreadable_files_say_why(self):
        draft = self.intake.extract(self.user, "x.pdf", b"not a pdf at all")
        self.assertEqual(draft["status"], "unreadable")
        self.assertIn("Upload a PDF", draft["message"])

    def test_bad_values_and_tampered_drafts(self):
        data, _ = self.samples["wayne-office-1042.pdf"]
        draft = self.intake.extract(self.user, "a.pdf", data)
        bad = self.intake.import_drafts(self.user, [self.checked(draft, date="2026-13-40")])
        self.assertEqual(bad["skipped"][0]["reason"], "the date is missing")
        forged = self.intake.import_drafts(self.user, [self.checked(draft, draft="nonsense")])
        self.assertIn("read the file again", forged["skipped"][0]["reason"])
        other_company = {"sub": "bob", "org": self.b, "role": "admin"}
        with self.assertRaises(IntakeError) as cm:
            self.intake.import_drafts(other_company, [self.checked(draft)])
        self.assertEqual(cm.exception.status, 403)


class AiReadingTests(IntakeTestCase):
    def ai_intake(self, post, enabled=True):
        settings = replace(self.settings, anthropic_api_key="sk-test")
        self.app.set_org_settings(self.a, ai_extraction=enabled)
        return DocumentIntake(self.app, self.signer, settings, ai_post=post, isolate=False)

    def test_only_used_when_switched_on_and_needed(self):
        calls = []
        post = lambda *a: calls.append(a) or answer()  # noqa: E731
        data, _ = self.samples["wayne-office-1042.pdf"]
        self.ai_intake(post, enabled=False).extract(self.user, "a.pdf", data)
        self.ai_intake(post, enabled=True).extract(self.user, "a.pdf", data)   # read fine locally
        self.assertEqual(calls, [])
        unreadable = self.ai_intake(post).extract(self.user, "photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        self.assertEqual(len(calls), 1)
        self.assertEqual(unreadable["fields"]["id"], "OCR-1")
        self.assertIn("Claude", unreadable["method"])
        self.assertIn("sent to Anthropic", str(self.app.list_audit(self.a)))

    def test_ai_failures_are_explained(self):
        def down(*a):
            raise OSError("no network")
        result = self.ai_intake(down).extract(self.user, "photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        self.assertEqual(result["status"], "unreadable")
