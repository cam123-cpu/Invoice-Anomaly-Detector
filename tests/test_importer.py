import io
import unittest
from datetime import datetime

from invoice_detector.importer import (FileProblem, convert, detect_date_order, parse_amount,
                                       parse_date, preview, read_table, suggest_mapping)


def xlsx_bytes(rows, title=None):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    if title:
        ws.append([title])
        ws.append([])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class AmountTests(unittest.TestCase):
    def test_formats(self):
        cases = {"1234.5": 1234.5, "$1,234.50": 1234.5, "1.234,50 €": 1234.5, "EUR 1 234,50": 1234.5,
                 "£12": 12.0, "1,234": 1234.0, "12,5": 12.5, "1.234.567": 1234567.0,
                 "USD 99.999": 100.0, " 7 ": 7.0, 42: 42.0, 19.999: 20.0, "1'234.50": 1234.5}
        for raw, expected in cases.items():
            self.assertEqual(parse_amount(raw), expected, raw)

    def test_rejects(self):
        for raw in ("", None, "abc", "0", "-5", "(12.00)", "12-", True, float("nan"), "1e13"):
            with self.assertRaises(ValueError, msg=repr(raw)):
                parse_amount(raw)


class DateTests(unittest.TestCase):
    def test_formats(self):
        cases = [("2026-03-05", "MDY", "2026-03-05"), ("2026/03/05", "DMY", "2026-03-05"),
                 ("03/05/2026", "MDY", "2026-03-05"), ("05/03/2026", "DMY", "2026-03-05"),
                 ("5.3.26", "DMY", "2026-03-05"), ("5 Mar 2026", "MDY", "2026-03-05"),
                 ("March 5, 2026", "DMY", "2026-03-05"), ("05-Mar-2026", "MDY", "2026-03-05"),
                 ("2026-03-05 00:00:00", "MDY", "2026-03-05"), ("2026-03-05T10:30", "MDY", "2026-03-05"),
                 (datetime(2026, 3, 5, 14, 0), "MDY", "2026-03-05"), (46086, "MDY", "2026-03-05")]
        for raw, order, expected in cases:
            self.assertEqual(parse_date(raw, order), expected, raw)

    def test_rejects(self):
        for raw in ("", None, "soon", "13/13/2026", "2026-02-30", 12, "01/01/1850"):
            with self.assertRaises(ValueError, msg=repr(raw)):
                parse_date(raw)

    def test_detect_order(self):
        self.assertEqual(detect_date_order(["03/05/2026", "25/12/2026"]), ("DMY", False))
        self.assertEqual(detect_date_order(["03/05/2026", "12/25/2026"]), ("MDY", False))
        self.assertEqual(detect_date_order(["03/05/2026", "04/06/2026"]), ("MDY", True))
        self.assertEqual(detect_date_order(["2026-03-05", datetime(2026, 1, 1)]), ("MDY", False))


class MappingTests(unittest.TestCase):
    def test_typical_exports(self):
        self.assertEqual(suggest_mapping(["Invoice #", "Supplier", "Invoice Date", "Total", "IBAN"]),
                         {"id": "Invoice #", "vendor": "Supplier", "date": "Invoice Date",
                          "amount": "Total", "bank_account": "IBAN", "kind": None})
        self.assertEqual(suggest_mapping(["Bill No", "Vendor", "Date", "Amount", "Document Type"])["kind"],
                         "Document Type")
        qb = suggest_mapping(["Date", "Transaction Type", "Num", "Name", "Memo", "Amount"])
        self.assertEqual((qb["date"], qb["vendor"], qb["amount"]), ("Date", "Name", "Amount"))
        fuzzy = suggest_mapping(["Supplier Name (legal)", "Doc Number", "Invoice Date", "Amount Due USD"])
        self.assertEqual(fuzzy["vendor"], "Supplier Name (legal)")
        self.assertEqual(fuzzy["amount"], "Amount Due USD")
        self.assertIsNone(fuzzy["bank_account"])

    def test_each_header_used_once(self):
        mapping = suggest_mapping(["Invoice", "Date", "Amount", "Vendor"])
        values = [v for v in mapping.values() if v]
        self.assertEqual(len(values), len(set(values)))


class ReadAndConvertTests(unittest.TestCase):
    ROWS = [["Invoice #", "Supplier", "Invoice Date", "Total", "IBAN"],
            ["INV/2026/001", "Acme Ltd", datetime(2026, 3, 5), 1234.5, "DE89 3704"],
            [1002.0, "Globex", "25/03/2026", "$2,000.00", None],
            [None, None, None, None, None],
            ["", "TOTAL", "", 3234.5, ""]]

    def test_xlsx_with_title_rows_and_messy_values(self):
        table = read_table("export.xlsx", xlsx_bytes(self.ROWS, title="Accounts payable - March"))
        self.assertEqual(table.headers[:2], ["Invoice #", "Supplier"])
        self.assertEqual(table.first_row_number, 4)
        info = preview(table)
        self.assertEqual(info["mapping"]["amount"], "Total")
        self.assertEqual((info["date_order"], info["date_order_ambiguous"]), ("DMY", False))
        result = convert(table, info["mapping"], info["date_order"])
        self.assertEqual(result.errors, [])
        self.assertEqual(result.blank_rows, 2)
        self.assertEqual(result.invoices, [
            {"id": "INV/2026/001", "vendor": "Acme Ltd", "amount": 1234.5, "date": "2026-03-05",
             "bank_account": "DE89 3704", "kind": "invoice"},
            {"id": "1002", "vendor": "Globex", "amount": 2000.0, "date": "2026-03-25", "bank_account": None,
             "kind": "invoice"}])

    def test_csv_semicolon_european_and_row_errors(self):
        text = ("Rechnung;Lieferant;Datum;Betrag\n"  # unknown German headers: mapped by hand
                "R-1;Müller GmbH;05.03.2026;1.234,50\n"
                "R-2;Schmidt AG;31.03.2026;0,00\n"
                "R-1;Müller GmbH;06.03.2026;10,00\n"
                "R 4!;X;06.03.2026;1\n"
                "G-9;Schmidt AG;31.03.2026;-5,00\n")
        table = read_table("export.csv", text.encode("cp1252"))
        mapping = {"id": "Rechnung", "vendor": "Lieferant", "date": "Datum", "amount": "Betrag"}
        result = convert(table, mapping, "DMY")
        self.assertEqual([(i["vendor"], i["amount"], i["kind"]) for i in result.invoices],
                         [("Müller GmbH", 1234.5, "invoice"), ("Schmidt AG", 5.0, "credit")])  # negative = credit note
        self.assertEqual([e["row"] for e in result.errors], [3, 4, 5])
        self.assertIn("zero", result.errors[0]["message"])
        self.assertIn("already appears on row 2", result.errors[1]["message"])

    def test_utf16_tab_separated(self):
        text = "Invoice No\tVendor\tDate\tAmount\r\nA1\tAcme\t2026-01-05\t10\r\n"
        table = read_table("x.txt", text.encode("utf-16"))
        self.assertEqual(len(convert(table, suggest_mapping(table.headers)).invoices), 1)

    def test_bad_files_and_mappings(self):
        with self.assertRaises(FileProblem):
            read_table("x.pdf", b"%PDF")
        with self.assertRaises(FileProblem):
            read_table("x.xls", b"\xd0\xcf")
        with self.assertRaises(FileProblem):
            read_table("x.xlsx", b"not really excel")
        with self.assertRaises(FileProblem):
            read_table("x.csv", b"")

    def test_files_must_be_what_their_name_says(self):
        import zipfile
        with self.assertRaisesRegex(FileProblem, "isn't an Excel workbook"):
            read_table("invoices.xlsx", b"MZ\x90\x00 a renamed program")
        with self.assertRaisesRegex(FileProblem, "isn't text"):
            read_table("invoices.csv", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        with self.assertRaisesRegex(FileProblem, "isn't text"):
            read_table("invoices.csv", xlsx_bytes(self.ROWS))  # a workbook renamed to .csv
        other_zip = io.BytesIO()
        with zipfile.ZipFile(other_zip, "w") as z:
            z.writestr("readme.txt", "hello")
        with self.assertRaisesRegex(FileProblem, "isn't an Excel workbook"):
            read_table("invoices.xlsx", other_zip.getvalue())

    def test_zip_bombs_are_refused_before_opening(self):
        import zipfile
        from unittest import mock
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/workbook.xml", "<workbook/>")
            z.writestr("xl/worksheets/sheet1.xml", b"\x00" * (8 * 1024 * 1024))  # 8 MB of zeros
        self.assertLess(len(bomb.getvalue()), 64 * 1024)  # tiny on disk
        # The real limit is 200 MB; a smaller one keeps the test fast.
        with mock.patch("invoice_detector.importer.MAX_UNZIPPED", 4 * 1024 * 1024):
            with self.assertRaisesRegex(FileProblem, "too large to read safely"):
                read_table("invoices.xlsx", bomb.getvalue())
        table = read_table("x.csv", b"Invoice,Vendor,Date,Amount\nA,B,2026-01-05,1\n")
        with self.assertRaisesRegex(FileProblem, "Invoice date"):
            convert(table, {"id": "Invoice", "vendor": "Vendor", "amount": "Amount"})
        with self.assertRaisesRegex(FileProblem, "only be used once"):
            convert(table, {"id": "Invoice", "vendor": "Invoice", "date": "Date", "amount": "Amount"})
        with self.assertRaisesRegex(FileProblem, "not in the file"):
            convert(table, {"id": "Nope", "vendor": "Vendor", "date": "Date", "amount": "Amount"})

    def test_duplicate_header_names_are_kept_apart(self):
        table = read_table("x.csv", b"Amount,Amount,Vendor,Date,Invoice\n1,2,V,2026-01-05,A\n")
        self.assertEqual(table.headers[:2], ["Amount", "Amount (2)"])



class CreditNoteImportTests(unittest.TestCase):
    def convert(self, headers, *rows):
        table = read_table("x.csv", "\n".join(",".join(r) for r in (headers, *rows)).encode())
        return convert(table, suggest_mapping(table.headers))

    def test_a_negative_amount_is_a_credit_note(self):
        result = self.convert(["Invoice No", "Vendor", "Date", "Amount"],
                              ["A1", "Acme", "2026-01-05", "100.00"],
                              ["CN1", "Acme", "2026-01-09", "-$38.20"],
                              ["CN2", "Acme", "2026-01-10", "(12.50)"],
                              ["CN3", "Acme", "2026-01-11", "7.00-"])
        self.assertEqual(result.errors, [])
        self.assertEqual([(i["id"], i["amount"], i["kind"]) for i in result.invoices],
                         [("A1", 100.0, "invoice"), ("CN1", 38.2, "credit"), ("CN2", 12.5, "credit"),
                          ("CN3", 7.0, "credit")])

    def test_a_type_column_marks_credit_notes(self):
        result = self.convert(["Type", "Invoice No", "Vendor", "Date", "Amount"],
                              ["Bill", "A1", "Acme", "2026-01-05", "100.00"],
                              ["Vendor Credit", "CN1", "Acme", "2026-01-09", "38.20"],
                              ["Credit Memo", "CN2", "Acme", "2026-01-09", "-5.00"],
                              ["Gutschrift", "CN3", "Acme", "2026-01-09", "5.00"])
        self.assertEqual([i["kind"] for i in result.invoices], ["invoice", "credit", "credit", "credit"])
        self.assertEqual([i["amount"] for i in result.invoices], [100.0, 38.2, 5.0, 5.0])   # stored positive

    def test_zero_is_still_refused(self):
        result = self.convert(["Invoice No", "Vendor", "Date", "Amount"], ["A1", "Acme", "2026-01-05", "0.00"])
        self.assertEqual((result.invoices, len(result.errors)), ([], 1))


if __name__ == "__main__":
    unittest.main()
