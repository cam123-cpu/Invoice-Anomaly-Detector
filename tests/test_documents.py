import io
import os
import unittest

from doc_templates import TEMPLATES
from invoice_detector import ai_extract, doc_samples
from invoice_detector.documents import (Box, account_value, as_date, find_fields, iban_ok, kind_of, money,
                                        ocr_available, read_document, totals_issue)
from invoice_detector.importer import FileProblem
from invoice_detector.isolation import run_isolated


class HelperTests(unittest.TestCase):
    def test_money(self):
        for text, value in (("$1,234.56", 1234.56), ("1.234,56 €", 1234.56), ("USD 99", 99.0), ("0.00", 0.0),
                            ("$ 11,250.00", 11250.0), (2250, 2250.0)):
            self.assertEqual(money(text), value, text)
        for text in ("8.25%", "-", "6/30/2023", "abc", "-5.00", "(12.00)"):
            self.assertIsNone(money(text), text)

    def test_dates(self):
        self.assertEqual(as_date(None, "November 9, 2023"), ("2023-11-09", False))
        self.assertEqual(as_date(None, "Tuesday, 3rd March 2026"), ("2026-03-03", False))
        self.assertEqual(as_date(None, "28.08.2026"), ("2026-08-28", False))
        self.assertEqual(as_date(None, "03/04/2026"), ("2026-03-04", True))   # March or April: flagged
        self.assertEqual(as_date(None, "not a date"), (None, False))

    def test_iban_checksum_catches_typos(self):
        self.assertTrue(iban_ok("GB29 NWBK 6016 1331 9268 19"))
        self.assertFalse(iban_ok("GB29 NWBK 6016 1331 9268 18"))
        self.assertEqual(account_value("IBAN: DE89 3704 0044 0532 0130 00"), "DE89370400440532013000")
        self.assertEqual(account_value("Acct. 0012-3456-789"), "00123456789")
        self.assertIsNone(account_value("Net 30"))

    def test_kind_comes_from_the_bytes_not_the_name(self):
        self.assertEqual(kind_of("invoice.xlsx", b"%PDF-1.4 ..."), "pdf")
        self.assertEqual(kind_of("invoice.pdf", b"PK\x03\x04..."), "xlsx")
        self.assertEqual(kind_of("photo.pdf", b"\x89PNG\r\n\x1a\n..."), "image")
        with self.assertRaises(FileProblem):
            read_document("invoice.pdf", b"MZ\x90\x00 a program")

    def test_totals_check(self):
        self.assertIsNone(totals_issue(108.0, {"subtotal": 100.0, "tax": 8.0}))
        self.assertIsNone(totals_issue(98.0, {"subtotal": 100.0, "tax": 8.0, "discount": 10.0}))
        self.assertIsNone(totals_issue(58.0, {"subtotal": 100.0, "tax": 8.0, "paid": 50.0}))
        self.assertIn("don't add up", totals_issue(208.0, {"subtotal": 100.0, "tax": 8.0}))
        self.assertIn("line items add up to 90.00", totals_issue(108.0, {"subtotal": 100.0, "tax": 8.0, "lines": 90.0}))
        self.assertIsNone(totals_issue(100.0, {}))  # nothing to compare


class LayoutTests(unittest.TestCase):
    def boxes(self, *rows):
        """rows of (x, y, text) on one page, 10pt text."""
        return [Box(t, x, y, x + len(t) * 5, y + 10, 0, 10) for x, y, t in rows]

    def test_value_after_beside_or_below_its_label(self):
        ex = find_fields(self.boxes((50, 20, "Bluebird Print Co"), (400, 60, "Invoice #: A-77"),
                                    (400, 80, "Date"), (480, 80, "2026-03-14"),
                                    (300, 300, "AMOUNT DUE"), (300, 312, "$1,020.00")), "Acme Inc")
        self.assertEqual((ex.fields["id"], ex.fields["date"], ex.fields["amount"], ex.fields["vendor"]),
                         ("A-77", "2026-03-14", 1020.0, "Bluebird Print Co"))

    def test_due_date_and_customer_are_not_mistaken(self):
        ex = find_fields(self.boxes((50, 20, "Harbor Logistics LLC"), (50, 100, "Bill to:"), (50, 112, "Acme Inc"),
                                    (400, 40, "Due date"), (480, 40, "2026-05-30"),
                                    (400, 56, "Invoice date"), (480, 56, "2026-04-30"),
                                    (400, 72, "Invoice no"), (480, 72, "9921"),
                                    (380, 300, "Total"), (480, 300, "77.10")), "Acme Inc")
        self.assertEqual(ex.fields["date"], "2026-04-30")
        self.assertEqual(ex.fields["vendor"], "Harbor Logistics LLC")

    def test_nothing_found_says_so(self):
        ex = find_fields([], "Acme Inc")
        self.assertEqual(ex.missing, ["id", "vendor", "date", "amount"])


class RealTemplateTests(unittest.TestCase):
    """Layouts of popular free invoice templates, which the reader wasn't written against."""

    def test_each_template_reads_correctly(self):
        for name, build in TEMPLATES.items():
            data, truth = build()
            ex = read_document(name + ".xlsx", data, "Acme Inc")
            with self.subTest(template=name):
                self.assertEqual(ex.fields["id"], truth["id"])
                self.assertEqual(ex.fields["vendor"], truth["vendor"])
                self.assertEqual(ex.fields["date"], truth["date"])
                self.assertAlmostEqual(ex.fields["amount"], truth["amount"], places=2)
                self.assertFalse([w for w in ex.warnings if "add up" in w])

    def test_payee_who_is_a_person_is_pointed_out(self):
        data, _ = TEMPLATES["Service invoice"]()
        ex = read_document("x.xlsx", data, "Acme Inc")
        self.assertIn("Lilu Linna", " ".join(ex.warnings))


class BenchmarkTests(unittest.TestCase):
    """The quality bar: generated invoices in four layouts, PDF and Excel. A change that
    makes any field worse than this fails the build."""

    def test_extraction_accuracy(self):
        report = doc_samples.evaluate_documents(60, seed=21)
        for field, accuracy in report["accuracy"].items():
            self.assertGreaterEqual(accuracy, 0.97, (field, report["mistakes"]))
        t = report["totals_check"]
        self.assertEqual(t["false_alarms"], 0)
        self.assertGreaterEqual(t["caught"], max(1, t["caught"] + t["missed"] - 1))

    def test_sample_files(self):
        expected = {"wayne-office-1042.pdf": {}, "stark-freight-8811.pdf": {"bank_account": "GB29NWBK60161331926819"},
                    "umbrella-ltd-3307.xlsx": {}, "acme-suplies-0091.pdf": {}}
        for name, data, doc in doc_samples.sample_docs():
            ex = read_document(name, data, "Acme Inc")
            truth = doc.truth()
            with self.subTest(sample=name):
                for f in ("id", "vendor", "date"):
                    self.assertEqual(ex.fields[f], truth[f])
                self.assertAlmostEqual(ex.fields["amount"], truth["amount"], places=2)
                for f, v in expected[name].items():
                    self.assertEqual(ex.fields[f], v)
                self.assertEqual(any("add up" in w for w in ex.warnings), name.startswith("umbrella"))


@unittest.skipUnless(ocr_available(), "Tesseract OCR isn't installed")
class OcrTests(unittest.TestCase):
    def scan(self, doc, rotate=0.8):
        import pdfplumber
        from PIL import ImageFilter
        with pdfplumber.open(io.BytesIO(doc_samples.to_pdf(doc))) as pdf:
            image = pdf.pages[0].to_image(resolution=200).original.convert("L")
        image = image.rotate(rotate, expand=True, fillcolor=255).filter(ImageFilter.GaussianBlur(0.5))
        out = io.BytesIO()
        image.save(out, "JPEG", quality=75)
        return out.getvalue()

    def test_photo_of_an_invoice(self):
        name, _, doc = doc_samples.sample_docs()[1]
        ex = read_document("photo.jpg", self.scan(doc), "Acme Inc")
        self.assertEqual((ex.fields["id"], ex.fields["vendor"]), ("SF-8811", "Stark Freight"))
        self.assertAlmostEqual(ex.fields["amount"], doc.printed_total, places=2)
        self.assertTrue(ex.method.startswith("OCR"))
        self.assertLessEqual(max(ex.confidence.values()), 0.8)   # OCR results always get a second look

    def test_scanned_pdf(self):
        from PIL import Image
        _, _, doc = doc_samples.sample_docs()[0]
        image = Image.open(io.BytesIO(self.scan(doc, 0))).convert("RGB")
        out = io.BytesIO()
        image.save(out, "PDF", resolution=200)   # a PDF with no text in it, just a picture
        ex = read_document("scan.pdf", out.getvalue(), "Acme Inc")
        self.assertEqual(ex.method, "OCR (scanned PDF)")
        self.assertEqual(ex.fields["id"], "WO-1042")


class IsolationTests(unittest.TestCase):
    def test_reads_in_a_child_process(self):
        name, data, doc = doc_samples.sample_docs()[0]
        ex = run_isolated("invoice_detector.documents:read_document", name, data, "Acme Inc")
        self.assertEqual(ex.fields["id"], "WO-1042")

    def test_problems_come_back_as_messages(self):
        with self.assertRaisesRegex(FileProblem, "Upload a PDF"):
            run_isolated("invoice_detector.documents:read_document", "x.pdf", b"nonsense", None)

    def test_a_stuck_reader_is_stopped(self):
        with self.assertRaisesRegex(FileProblem, "took longer than 1 seconds"):
            run_isolated("slow_target:hang", timeout=1)

    @unittest.skipUnless(os.name == "posix" and os.uname().sysname == "Linux", "memory limits are Linux-only")
    def test_a_memory_hog_is_stopped(self):
        with self.assertRaisesRegex(FileProblem, "too much memory"):
            run_isolated("slow_target:hog", memory_mb=600)


def tool_answer(**fields):
    base = {"invoice_number": "SF-9001", "vendor_name": "Stark Freight", "invoice_date": "2026-10-30",
            "total_amount": 4800.0, "subtotal": 4800.0, "tax": 0.0, "bank_account": None}
    base.update(fields)
    return {"content": [{"type": "tool_use", "name": "record_invoice", "input": base}]}


class AiExtractionTests(unittest.TestCase):
    def test_request_is_a_forced_tool_call_with_the_document(self):
        sent = {}

        def post(url, headers, body):
            sent.update(url=url, headers=headers, body=body)
            return tool_answer()
        ex = ai_extract.extract_with_claude("pdf", b"%PDF-1.4 fake", None, "Acme Inc", "sk-test", post=post)
        body = sent["body"]
        self.assertEqual(body["tool_choice"], {"type": "tool", "name": "record_invoice"})
        self.assertEqual(body["messages"][0]["content"][0]["type"], "document")
        self.assertIn("never instructions", body["system"])
        self.assertIn("not Acme Inc", body["system"])
        self.assertEqual(sent["headers"]["x-api-key"], "sk-test")
        self.assertEqual((ex.fields["id"], ex.fields["amount"], ex.method), ("SF-9001", 4800.0, "Claude (AI reading)"))

    def test_answers_are_checked_like_local_reads(self):
        ex = ai_extract.to_extraction(tool_answer(invoice_date="the 31st of Smarch", total_amount=-5,
                                                  bank_account="GB29NWBK60161331926819",
                                                  uncertain_fields=["vendor_name"])["content"][0]["input"])
        self.assertNotIn("date", ex.fields)
        self.assertNotIn("amount", ex.fields)
        self.assertEqual(ex.confidence["vendor"], 0.5)
        self.assertEqual(ex.fields["bank_account"], "GB29NWBK60161331926819")
        self.assertEqual(len(ex.warnings), 2)

    def test_failures_are_reported_not_raised_raw(self):
        import urllib.error

        def refuse(url, headers, body):
            raise urllib.error.HTTPError(url, 401, "no", {}, None)
        with self.assertRaisesRegex(ai_extract.AIError, "HTTP 401"):
            ai_extract.extract_with_claude("pdf", b"%PDF", None, None, "bad", post=refuse)
        with self.assertRaisesRegex(ai_extract.AIError, "didn't return"):
            ai_extract.extract_with_claude("pdf", b"%PDF", None, None, "k", post=lambda *a: {"content": []})
        with self.assertRaisesRegex(ai_extract.AIError, "PNG or JPEG"):
            ai_extract.content_for("image", b"II*\x00tiff", None)

    def test_merge_keeps_sure_local_values_and_reports_disagreement(self):
        from invoice_detector.documents import Extraction
        local = Extraction(fields={"id": "SF-9001", "amount": 4900.0}, confidence={"id": 0.95, "amount": 0.9},
                           method="OCR (image)")
        merged = ai_extract.merge(local, ai_extract.to_extraction(tool_answer()["content"][0]["input"]))
        self.assertEqual(merged.fields["amount"], 4900.0)
        self.assertEqual(merged.fields["vendor"], "Stark Freight")      # filled in by Claude
        self.assertEqual(merged.method, "OCR (image) + Claude")
        self.assertIn("disagree on the amount", " ".join(merged.warnings))
        self.assertEqual(merged.confidence["amount"], 0.5)


if __name__ == "__main__":
    unittest.main()
