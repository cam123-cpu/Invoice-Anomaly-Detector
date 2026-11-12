import io
import os
import unittest

from doc_templates import TEMPLATES
from invoice_detector import ai_extract, doc_samples
from invoice_detector.documents import (Box, account_value, as_date, find_fields, iban_ok, kind_of, match_label, money,
                                        ocr_available, parse_value, read_document, totals_issue)
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
                    "umbrella-ltd-3307.xlsx": {}, "acme-suplies-0091.pdf": {},
                    "globex-corp-7730.pdf": {}, "globex-corp-credit-note-118.pdf": {}}
        for name, data, doc in doc_samples.sample_docs():
            ex = read_document(name, data, "Acme Inc")
            truth = doc.truth()
            with self.subTest(sample=name):
                for f in ("id", "vendor", "date"):
                    self.assertEqual(ex.fields[f], truth[f])
                self.assertAlmostEqual(ex.fields["amount"], truth["amount"], places=2)
                for f, v in expected[name].items():
                    self.assertEqual(ex.fields[f], v)
                self.assertEqual(ex.kind, "credit" if "credit-note" in name else "invoice")
                self.assertEqual(any("add up" in w for w in ex.warnings), name.startswith("umbrella"))


def page(*rows):
    """Boxes for a page given as rows of (x, text): one row per printed line."""
    boxes = []
    for line, row in enumerate(rows):
        for x, text in row:
            boxes.append(Box(text, x, 40 + line * 14, x + len(text) * 5.5, 50 + line * 14, 0, 10))
    return boxes


class OtherLanguagesTests(unittest.TestCase):
    """Invoices that aren't in English, laid out the way real ones are."""

    def test_german(self):
        ex = find_fields(page(
            [(40, "Muster Hosting AG - Uferweg 40-42 - D-63571 Gelnhausen")],
            [(40, "Kunden AG Mitte")], [(40, "Kundenstraße 15")], [(40, "69876 Frankfurt")],
            [(40, "Rechnung"), (400, "Seite 1")],
            [(40, "Rechnungsnr. 30064443"), (300, "Kundennr. 47774")],
            [(40, "Rechnungsdatum 7. Mai 2014"), (300, "Lieferdatum 02.05.2014")],
            [(40, "Pos."), (80, "Bezeichnung"), (300, "Menge"), (400, "Betrag")],
            [(40, "1"), (80, "Grundgebühr"), (300, "1"), (400, "100,00")],
            [(300, "Nettobetrag"), (400, "100,00 €")],
            [(300, "MwSt 19%"), (400, "19,00 €")],
            [(300, "Gesamtbetrag"), (400, "119,00 €")],
            [(40, "IBAN DE89 3704 0044 0532 0130 00")]), "Kunden AG Mitte")
        self.assertEqual(ex.fields, {"id": "30064443", "date": "2014-05-07", "amount": 119.0,
                                     "vendor": "Muster Hosting AG", "bank_account": "DE89370400440532013000"})
        self.assertEqual((ex.totals["subtotal"], ex.totals["tax"]), (100.0, 19.0))
        self.assertFalse(any("add up" in w for w in ex.warnings))

    def test_a_title_that_carries_the_number_and_date(self):
        ex = find_fields(page(
            [(40, "Handelsrechnung (380) Nr. 471102 vom 05.06.2018")],
            [(40, "Bestellung:"), (260, "2018-471331")],
            [(40, "Verkäufer")], [(40, "Name:"), (260, "Lieferant GmbH")], [(40, "Anschrift:"), (260, "Lieferantenstraße 20")],
            [(40, "Käufer")], [(40, "Name:"), (260, "Kunden AG Mitte")],
            [(40, "Rechnung gemäß Bestellung Nr. 2018-471331 vom 01.03.2018.")],
            [(40, "Rechnungsbetrag"), (260, "235,62")]))
        self.assertEqual((ex.fields["id"], ex.fields["date"], ex.fields["vendor"], ex.fields["amount"]),
                         ("471102", "2018-06-05", "Lieferant GmbH", 235.62))

    def test_french(self):
        ex = find_fields(page(
            [(40, "Au bon moulin SARL - 1242 chemin de l'olive - 84340 Malaucène - France")],
            [(40, "Facture FA-2017-0010")],
            [(300, "Ma jolie boutique")], [(300, "35 rue de la République")], [(300, "69001 Lyon")],
            [(40, "Date de facture 13/11/2017 Contact commercial")],
            [(40, "Date d'échéance 13/12/2017")],
            [(300, "Total HT"), (420, "624,90 €")],
            [(300, "Total taxes"), (420, "46,25 €")],
            [(300, "Total TTC"), (420, "671,15 €")]))
        self.assertEqual((ex.fields["id"], ex.fields["date"], ex.fields["vendor"], ex.fields["amount"]),
                         ("FA-2017-0010", "2017-11-13", "Au bon moulin SARL", 671.15))
        inline = find_fields(page([(40, "Facture n°562044387 du 02 Juillet 2015")],
                                  [(40, "Total facture"), (300, "24.99"), (360, "5.00"), (420, "29.99")]))
        self.assertEqual((inline.fields["id"], inline.fields["date"], inline.fields["amount"]),
                         ("562044387", "2015-07-02", 29.99))     # net, tax, gross: the gross is the total

    def test_dutch_with_a_kerned_capital(self):
        ex = find_fields(page(
            [(40, "FACTUUR."), (300, "Coolblue B.V.")], [(300, "Weena 664")], [(300, "3012 CN Rotterdam")],
            [(40, "F actuurnummer: 993548900")], [(40, "K lantnummer: 6669263")],
            [(40, "F actuurdatum: 19 april 2014")], [(40, "Orderdatum: 18 april 2014")],
            [(40, "Totaal € 717,97")]))
        self.assertEqual(ex.fields, {"id": "993548900", "date": "2014-04-19", "amount": 717.97, "vendor": "Coolblue B.V."})

    def test_spanish_and_italian_labels(self):
        es = find_fields(page([(40, "Olivos del Sur S.L.")], [(40, "Número de factura: F-2024-118")],
                              [(40, "Fecha de factura: 5 de marzo de 2024")], [(40, "Fecha de vencimiento: 04/04/2024")],
                              [(40, "Base imponible"), (300, "200,00")], [(40, "IVA 21%"), (300, "42,00")],
                              [(40, "Total a pagar"), (300, "242,00 €")]))
        self.assertEqual((es.fields["id"], es.fields["date"], es.fields["amount"], es.fields["vendor"]),
                         ("F-2024-118", "2024-03-05", 242.0, "Olivos del Sur S.L."))
        it = find_fields(page([(40, "Officine Rossi S.r.l.")], [(40, "Fattura n° 2024/57")],
                              [(40, "Data fattura: 12/09/2024")], [(40, "Imponibile"), (300, "1.000,00")],
                              [(40, "Totale fattura"), (300, "1.220,00")]))
        self.assertEqual((it.fields["id"], it.fields["date"], it.fields["amount"]), ("2024/57", "2024-09-12", 1220.0))

    def test_dates(self):
        self.assertEqual(as_date(None, "7. Mai 2014"), ("2014-05-07", False))
        self.assertEqual(as_date(None, "02 Juillet 2015"), ("2015-07-02", False))
        self.assertEqual(as_date(None, "1er août 2023"), ("2023-08-01", False))
        self.assertEqual(as_date(None, "August 3 , 2014"), ("2014-08-03", False))
        self.assertEqual(as_date(None, "05.06.2018"), ("2018-06-05", False))        # dots: always day first
        self.assertEqual(as_date(None, "05/06/2018"), ("2018-05-06", True))         # no other clue: flagged
        # Another date on the page settles the order; so does the language.
        for clue in ("Delivered 28/05/2018", "MwSt 19% Rechnungsdatum"):
            ex = find_fields(page([(40, "Invoice No: 77")], [(40, "Invoice date: 05/06/2018")], [(40, clue)]))
            self.assertEqual((ex.fields["date"], ex.confidence["date"] > 0.9), ("2018-06-05", True), clue)
        us = find_fields(page([(40, "Invoice No: 77")], [(40, "Invoice date: 05/06/2018")], [(40, "Due 05/28/2018")]))
        self.assertEqual(us.fields["date"], "2018-05-06")
        pounds = find_fields(page([(40, "Invoice No: 77")], [(40, "Invoice date: 05/06/2018")], [(40, "Total £10.00")]))
        self.assertEqual(pounds.fields["date"], "2018-06-05")                         # likely, and still flagged
        self.assertTrue(any("two ways" in w for w in pounds.warnings))

    def test_money_and_labels(self):
        self.assertEqual((money("29.99 € TTC"), money("120 000,00 €"), money("EUR 49,99")), (29.99, 120000.0, 49.99))
        self.assertEqual(match_label("Lieferant GmbH", "payable_to"), None)           # a company, not a label
        self.assertEqual(match_label("Lieferant: Muster KG", "payable_to"), (3, "Muster KG"))
        self.assertIsNone(match_label("Date d'échéance 13/12/2017", "date"))
        self.assertIsNone(match_label("Kundennr. 47774", "id"))
        self.assertIsNone(match_label("USt-IdNr. DE123456789", "tax"))
        self.assertEqual(match_label("Invoice No. 123", "id"), (3, "123"))
        self.assertEqual(parse_value("id", "471102 vom 05.06.2018"), ("471102", 1.0))
        self.assertEqual(parse_value("id", "INV 2024 001"), ("INV 2024 001", 1.0))
        self.assertEqual(parse_value("tax", "20,0% 81,90 € 16,38 €")[0], 16.38)

    def test_the_totals_check_stays_quiet_when_it_cannot_be_sure(self):
        self.assertIsNone(totals_issue(215.14, {"subtotal": 202.76, "tax": 123456789.0}))       # that's a tax number
        self.assertIsNone(totals_issue(56.02, {"subtotal": 46.68}))                              # 20% tax, line unread
        self.assertIsNone(totals_issue(571.04, {"subtotal": 496.0, "tax_unread": True}))         # mixed rates, unread
        self.assertIsNone(totals_issue(470.15, {"subtotal": 624.9, "tax": 46.25, "printed_totals": [671.15]}))
        self.assertIn("add up", totals_issue(8213.62, {"subtotal": 7986.07}))                    # 2.85% is no tax rate
        self.assertIn("add up", totals_issue(996.4, {"subtotal": 830.0, "tax": 66.4}))
        self.assertIn("add up", totals_issue(996.4, {"subtotal": 830.0, "tax": 66.4, "printed_totals": [500.0]}))

    def test_a_total_column_heading_is_not_the_invoice_total(self):
        ex = find_fields(page(
            [(40, "Bei Spiel GmbH")], [(40, "Invoice # RE-20190610/507 issued at 2019-06-10")],
            [(40, "Amount"), (120, "Product"), (290, "VAT"), (350, "Price"), (420, "Total")],
            [(40, "1.00"), (120, "Design"), (290, "7%"), (350, "160.00 €"), (420, "160.00 €")],
            [(40, "400.00"), (120, "Ballons"), (290, "19%"), (350, "0.84 €"), (420, "336.00 €")],
            [(40, "Net total: 496.00 €")], [(150, "VAT total: 75.04 €")], [(40, "Due payable: 571.04 €")]))
        self.assertEqual((ex.fields["id"], ex.fields["amount"], ex.totals.get("lines")), ("RE-20190610/507", 571.04, 496.0))
        self.assertFalse(any("add up" in w for w in ex.warnings))

    def test_words_are_joined_the_way_they_are_printed(self):
        from invoice_detector.documents import _merge_words

        def word(text, x0, x1, size=10):
            return {"text": text, "x0": x0, "x1": x1, "top": 100, "bottom": 110, "size": size}
        # typewriter font: a space is 0.6 of the font size and must not split the phrase
        self.assertEqual([b.text for b in _merge_words([word("Lieferant", 50, 104), word("GmbH", 110, 134)], 0)],
                         ["Lieferant GmbH"])
        # a number the PDF stores in two pieces, with no gap between them
        self.assertEqual([b.text for b in _merge_words([word("Nr.", 50, 70), word("4", 76, 82), word("71102", 82, 112)], 0)],
                         ["Nr. 471102"])
        # a real column gap still separates
        self.assertEqual(len(_merge_words([word("Total", 50, 80), word("119,00", 300, 340)], 0)), 2)

    def test_credit_note_titles(self):
        from invoice_detector.documents import is_credit_note
        for title, credit in (("Rechnungskorrektur", True), ("Avoir AV-2017-0005", True), ("Creditnota", True),
                              ("Gutschrift", True), ("Gutschrift (Selbst ausgestellte Rechnung)", False),
                              ("Handelsrechnung", False), ("Facture", False)):
            self.assertEqual(is_credit_note(page([(40, title)], [(40, "Nr. 5")])), credit, title)


class VendorAndCreditRulesTests(unittest.TestCase):
    def test_the_account_holder_in_a_packed_footer_line_is_the_vendor(self):
        ex = find_fields(page([(380, "ALEXINUX")], [(380, "27 rue Henri Rolland")], [(380, "69100 Villeurbanne")],
                              [(240, "Facture n° 2022089083")], [(56, "Date : 28/11/2022")],
                              [(56, "Total TTC"), (400, "49,00 €")],
                              [(120, "BRED BANQUE POPULAIRE – Titulaire du compte : NETPRESSE")], [(278, "NETPRESSE")]))
        self.assertEqual(ex.fields["vendor"], "NETPRESSE")

    def test_sold_by_names_the_seller(self):
        ex = find_fields(page([(10, "Sold By : WS Retail Services Pvt. Ltd.,")], [(10, "Invoice No: FAKE-1")],
                              [(10, "Invoice Date: 2016-02-15")], [(300, "Anushrut Singh")], [(10, "Total"), (400, "730.00")]))
        self.assertEqual(ex.fields["vendor"], "WS Retail Services Pvt. Ltd.")

    def test_a_payee_who_is_a_person_still_gives_way_to_the_business(self):
        ex = find_fields(page([(40, "Bright Windows Cleaning")], [(40, "Invoice # 51")], [(40, "Date: 2026-01-05")],
                              [(40, "Make all checks payable to John Smith")], [(40, "Total"), (300, "$80.00")]))
        self.assertEqual(ex.fields["vendor"], "Bright Windows Cleaning")
        self.assertTrue(any("John Smith" in w for w in ex.warnings))

    def test_sentences_and_item_lines_are_not_names(self):
        from invoice_detector.documents import looks_like_name
        for text in ("Prijzen zijn inclusief BTW", "Rs 1939 x 1 Night x 1 Room", "Retail Invoices/Bill",
                     "Preise sind Endpreise", "Thank you for your order"):
            self.assertFalse(looks_like_name(text), text)
        for text in ("Coolblue B.V.", "OYO 4189 Resort Nanganallur", "e-Luscious Nederland B.V."):
            self.assertTrue(looks_like_name(text), text)

    def test_a_document_whose_only_total_is_negative_is_a_credit(self):
        ex = find_fields(page([(40, "Muster GmbH")], [(40, "Korrektur Nr. RK-1 vom 16.09.2019")],
                              [(40, "Endbetrag"), (300, "-8,79")]))
        self.assertEqual((ex.kind, ex.fields["amount"]), ("credit", 8.79))
        self.assertTrue(any("negative" in w for w in ex.warnings))
        plain = find_fields(page([(40, "Muster GmbH")], [(40, "Rechnung Nr. 5")], [(40, "Endbetrag"), (300, "8,79")]))
        self.assertEqual(plain.kind, "invoice")


class RealFolderBenchmarkTests(unittest.TestCase):
    def test_a_folder_with_answers_is_scored(self):
        import tempfile
        from pathlib import Path
        from invoice_detector.doc_benchmark import format_report, score_folder, vendor_matches
        with tempfile.TemporaryDirectory() as tmp:
            rows = ["file,id,vendor,date,amount,amount_due,kind"]
            for name, data, doc in doc_samples.sample_docs():
                (Path(tmp) / name).write_bytes(data)
                t = doc.truth()
                rows.append(f"{name},{t['id']},{t['vendor']},{t['date']},{t['amount']},,{t['kind']}")
            rows.append("../outside.pdf,1,X,2026-01-01,1,,")          # never read: outside the folder
            rows.append("missing.pdf,1,X,2026-01-01,1,,")
            (Path(tmp) / "copy.pdf").write_bytes(doc_samples.sample_docs()[0][1])
            rows.append(rows[1].replace(doc_samples.sample_docs()[0][0], "copy.pdf"))   # same bytes: counted once
            (Path(tmp) / "truth.csv").write_text("\n".join(rows), encoding="utf-8")
            report = score_folder(tmp, isolate=False)
        self.assertEqual((report["documents"], report["all_four"], report["kind"]), (6, 1.0, 1.0))
        self.assertEqual(report["totals_warnings"], 1)                 # the sample whose totals really are wrong
        self.assertIn("all four right: 100%", format_report(report))
        self.assertTrue(vendor_matches("Acme Supplies Ltd", "Acme Supplies"))
        self.assertFalse(vendor_matches("Rechnung", "Acme Supplies"))


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
