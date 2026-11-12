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


class CreditNoteIntakeTests(IntakeTestCase):
    def test_a_credit_note_document_is_read_as_one(self):
        data, doc = self.samples["globex-corp-credit-note-118.pdf"]
        draft = self.intake.extract(self.user, "cn.pdf", data)
        self.assertEqual((draft["status"], draft["kind"]), ("ready", "credit"))
        self.assertAlmostEqual(draft["fields"]["amount"], doc.truth()["amount"], places=2)
        self.assertTrue(any("credit note" in w for w in draft["warnings"]))
        result = self.intake.import_drafts(self.user, [self.checked(draft, kind=draft["kind"])])
        self.assertEqual((result["added"], result["fields_corrected"]), (1, 0))
        stored = self.app.list_invoices(self.a)[0]
        self.assertEqual((stored["kind"], stored["amount"]), ("credit", -draft["fields"]["amount"]))

    def test_changing_the_type_by_hand_is_recorded(self):
        data, _ = self.samples["wayne-office-1042.pdf"]
        draft = self.intake.extract(self.user, "a.pdf", data)
        self.assertEqual(draft["kind"], "invoice")
        result = self.intake.import_drafts(self.user, [self.checked(draft, kind="credit")])
        self.assertEqual(result["fields_corrected"], 1)
        self.assertEqual(self.app.list_invoices(self.a)[0]["kind"], "credit")
        record = self.admin.conn.execute("SELECT corrections FROM documents").fetchone()["corrections"]
        self.assertIn("kind", record)

    def test_a_negative_amount_is_refused_whatever_the_type(self):
        data, _ = self.samples["wayne-office-1042.pdf"]
        draft = self.intake.extract(self.user, "a.pdf", data)
        result = self.intake.import_drafts(self.user, [self.checked(draft, kind="credit", amount=-5)])
        self.assertEqual(result["added"], 0)


class EInvoiceIntakeTests(IntakeTestCase):
    def test_an_xml_credit_note_naming_a_missing_invoice(self):
        from invoice_detector.detection import detect
        from test_einvoice import cii
        draft = self.intake.extract(self.user, "gutschrift.xml", cii(id="GS-9", type="381", ref="RE-404"))
        self.assertEqual((draft["status"], draft["kind"], draft["fields"]["reference"]), ("ready", "credit", "RE-404"))
        result = self.intake.import_drafts(self.user, [self.checked(draft, kind="credit")])
        self.assertEqual((result["added"], result["fields_corrected"]), (1, 0))
        self.assertEqual(self.app.list_invoices(self.a)[0]["reference"], "RE-404")
        flags = {(a.invoice_id, a.rule): a.detail for a in detect(self.app.detection_inputs(self.a))}
        self.assertIn("isn't on file", flags[("GS-9", "unmatched_credit")])

    def test_a_reference_is_kept_only_on_credit_notes_and_must_look_like_a_number(self):
        from test_einvoice import cii
        draft = self.intake.extract(self.user, "a.xml", cii(id="RE-1"))
        bad = self.intake.extract(self.user, "b.xml", cii(id="RE-2", type="381"))
        self.intake.import_drafts(self.user, [self.checked(draft, reference="RE-0"),
                                              self.checked(bad, kind="credit", reference="<script>")])
        stored = {i["id"]: i["reference"] for i in self.app.list_invoices(self.a)}
        self.assertEqual(stored, {"RE-1": None, "RE-2": None})


def deposit_invoice(vendor: str, number: str, total: float, due: float) -> bytes:
    """A vendor that prints the balance after a deposit high up, and the full total lower down."""
    from invoice_detector.doc_samples import pdf_bytes
    t = lambda y, s, x=50, size=10, bold=False: ("text", x, y, size, bold, s)  # noqa: E731
    return pdf_bytes([t(40, vendor, size=16, bold=True), t(62, "12 Harbour Road, Leeds LS1 4AB"),
                      t(110, f"Invoice number: {number}"), t(126, "Invoice date: 2026-03-05"),
                      t(150, f"Balance due {due:,.2f}", bold=True),
                      t(200, "Bill to: Acme Inc"),
                      t(260, "Freight services"), t(260, f"{total:,.2f}", x=450),
                      t(400, "Total"), t(400, f"{total:,.2f}", x=450),
                      t(416, "Deposit received"), t(416, f"{total - due:,.2f}", x=450)])


class LearningTests(IntakeTestCase):
    def test_one_correction_teaches_the_reader_this_vendor(self):
        first = self.intake.extract(self.user, "a.pdf", deposit_invoice("Harbour Freight Ltd", "HF-1", 1200.0, 900.0))
        self.assertEqual(first["fields"]["amount"], 900.0)           # takes the balance due...
        # ...but this company records the full invoice total (deposits are booked separately)
        result = self.intake.import_drafts(self.user, [self.checked(first, amount=1200.0)])
        self.assertEqual(result["fields_corrected"], 1)
        self.assertEqual(self.app.reading_hints(self.a), {"harbour freight": {"amount": ["total"]}})

        second = self.intake.extract(self.user, "b.pdf", deposit_invoice("Harbour Freight Ltd", "HF-2", 700.0, 500.0))
        self.assertEqual(second["fields"]["amount"], 700.0)
        self.assertTrue(any("where you corrected it" in w for w in second["warnings"]))
        other = self.intake.extract(self.user, "c.pdf", deposit_invoice("Other Carrier Ltd", "OC-1", 700.0, 500.0))
        self.assertEqual(other["fields"]["amount"], 500.0)           # other vendors: unchanged

        # Kept once (confirmed), then corrected back to the balance three times: the reader
        # drops the old lesson and learns the new one.
        self.intake.import_drafts(self.user, [self.checked(second)])
        for n in (3, 4, 5):
            draft = self.intake.extract(self.user, f"{n}.pdf",
                                        deposit_invoice("Harbour Freight Ltd", f"HF-{n}", 300.0 + n, 100.0))
            self.assertEqual(draft["fields"]["amount"], 300.0 + n if n == 3 else 100.0)   # from the 4th, the new lesson leads
            self.intake.import_drafts(self.user, [self.checked(draft, amount=100.0)])
        self.assertEqual(self.app.reading_hints(self.a), {"harbour freight": {"amount": ["balance due", "total"]}})

    def test_lessons_stay_inside_their_company(self):
        draft = self.intake.extract(self.user, "a.pdf", deposit_invoice("Harbour Freight Ltd", "HF-1", 1200.0, 900.0))
        self.intake.import_drafts(self.user, [self.checked(draft, amount=1200.0)])
        other_company = {"sub": "carol", "org": self.b, "role": "admin"}
        theirs = self.intake.extract(other_company, "b.pdf", deposit_invoice("Harbour Freight Ltd", "HF-2", 700.0, 500.0))
        self.assertEqual(theirs["fields"]["amount"], 500.0)
        self.assertEqual(self.app.reading_hints(self.b), {})

    def test_a_tampered_lesson_cannot_be_sent_back(self):
        """Candidates ride in the signed draft: changing them breaks the signature."""
        draft = self.intake.extract(self.user, "a.pdf", deposit_invoice("Harbour Freight Ltd", "HF-1", 1200.0, 900.0))
        payload, sig = draft["draft"].rsplit(".", 1)
        forged = payload[:-4] + ("AAAA" if payload[-4:] != "AAAA" else "BBBB") + "." + sig
        result = self.intake.import_drafts(self.user, [self.checked(draft, draft=forged, amount=1200.0)])
        self.assertEqual(result["added"], 0)
        self.assertEqual(self.app.reading_hints(self.a), {})
