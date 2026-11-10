import io
import unittest
from datetime import datetime

from openpyxl import Workbook, load_workbook

from invoice_detector.excel_out import ANNOTATION_HEADERS, annotate, payment_file, review_report, safe, template
from invoice_detector.importer import convert, preview, read_table

EVIL = '=HYPERLINK("http://evil.example","click")'


def workbook(sheets: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
        for row in ws.iter_rows():
            for cell in row:
                if cell.value == EVIL:  # stored as the text a vendor typed, not as a formula
                    cell.data_type = "s"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def item(inv_id, risk, rules, status="open"):
    return {"invoice_id": inv_id, "risk": risk, "rules": rules, "details": [f"{r} detail" for r in rules],
            "status": status, "vendor": "V", "amount": 10.0, "date": "2026-01-05"}


class AnnotateTests(unittest.TestCase):
    def setUp(self):
        self.data = workbook({
            "Notes": [["This sheet only has notes"]],
            "AP": [["Accounts payable"], [],
                   ["Invoice #", "Supplier", "Date", "Total", "Approver"],
                   ["A1", "Acme", datetime(2026, 1, 5), 100.0, "kim"],
                   ["A2", EVIL, datetime(2026, 1, 5), 100.0, "=UPPER(\"lee\")"],
                   ["A3", "Globex", "not a date", 5.0, None],
                   ["A4", "Initech", datetime(2026, 1, 7), 7.0, None]]})
        self.table = read_table("ap.xlsx", self.data)
        info = preview(self.table)
        self.result = convert(self.table, info["mapping"], info["date_order"])
        queue = {"A2": item("A2", 85, ["duplicate"], "confirmed"), "A4": item("A4", 30, ["weekend_invoice"])}
        out, self.name, self.media = annotate("ap.xlsx", self.data, self.table, self.result.lines,
                                              self.result.errors, queue, "Acme Inc")
        self.wb = load_workbook(io.BytesIO(out))

    def test_finds_the_sheet_with_the_table(self):
        self.assertEqual((self.table.sheet, self.table.sheets), ("AP", ["AP"]))
        self.assertEqual(self.result.lines, {"A1": 4, "A2": 5, "A4": 7})

    def test_results_are_written_beside_each_row(self):
        ws = self.wb["AP"]
        self.assertEqual([ws.cell(3, c).value for c in range(6, 12)], ANNOTATION_HEADERS)
        self.assertEqual((ws.cell(4, 7).value, ws.cell(4, 8).value), ("No flags", "Pay"))
        self.assertEqual([ws.cell(5, c).value for c in range(6, 10)],
                         [85, "Confirmed problem", "Do not pay", "Duplicate"])
        self.assertTrue(ws.cell(5, 11).value.startswith("Don't pay"))  # what to do
        self.assertEqual(ws.cell(5, 6).fill.fgColor.rgb[-6:], "FDE2E0")  # red for high risk
        self.assertEqual((ws.cell(6, 7).value, ws.cell(6, 10).value[:4]), ("Not imported", "date"))
        self.assertEqual(ws.cell(7, 8).value, "Review first")  # flagged but not reviewed
        self.assertEqual(ws.cell(7, 6).fill.fgColor.rgb[-6:], "EEF0F3")  # grey for low risk
        self.assertEqual(self.wb.active.title, "AP")
        self.assertEqual((self.name, self.media.endswith("sheet")), ("ap - checked.xlsx", True))

    def test_original_cells_and_formulas_are_untouched(self):
        ws = self.wb["AP"]
        self.assertEqual(ws.cell(5, 5).value, '=UPPER("lee")')  # the user's own formula survives
        self.assertEqual(ws.cell(1, 1).value, "Accounts payable")
        self.assertEqual(self.wb["Notes"]["A1"].value, "This sheet only has notes")

    def test_added_text_can_never_become_a_formula(self):
        flagged = self.wb["Flagged invoices"]
        self.assertEqual([c.value for c in flagged[1]][:3], ["Risk", "Invoice", "Vendor"])
        self.assertEqual(flagged.cell(2, 2).value, "A2")  # highest risk first
        for ws in self.wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if cell.coordinate == "E5" and ws.title == "AP":
                        continue  # the user's own formula, deliberately kept
                    self.assertNotEqual(cell.data_type, "f", f"{ws.title}!{cell.coordinate}")
        self.assertEqual(safe(EVIL), "'" + EVIL)
        self.assertEqual(safe("-5"), "'-5")
        self.assertEqual(safe(-5), -5)
        self.assertIn("About these flags", self.wb.sheetnames)

    def test_csv_comes_back_as_a_workbook(self):
        data = b"Invoice,Vendor,Date,Amount\nC1,Acme,2026-01-05,10\nC2,=1+2 Corp,2026-01-05,10\n"
        table = read_table("export.csv", data)
        result = convert(table, preview(table)["mapping"])
        out, name, _ = annotate("export.csv", data, table, result.lines, result.errors,
                                {"C2": item("C2", 50, ["duplicate"])}, "Acme Inc")
        ws = load_workbook(io.BytesIO(out))["Invoices"]
        self.assertEqual(name, "export - checked.xlsx")
        self.assertEqual(ws.cell(3, 2).data_type, "s")
        self.assertTrue(ws.cell(3, 2).value.startswith("'="))
        self.assertEqual(ws.cell(3, 5).value, 50)

    def test_pick_a_specific_sheet(self):
        data = workbook({"Q1": [["Invoice", "Vendor", "Date", "Amount"], ["Q1-1", "A", "2026-01-05", 1]],
                         "Q2": [["Invoice", "Vendor", "Date", "Amount"], ["Q2-1", "B", "2026-04-05", 2],
                                ["Q2-2", "B", "2026-04-06", 3]]})
        self.assertEqual(read_table("x.xlsx", data).sheets, ["Q1", "Q2"])
        q2 = read_table("x.xlsx", data, sheet="Q2")
        self.assertEqual((q2.sheet, len(q2.rows)), ("Q2", 2))
        with self.assertRaises(ValueError):
            read_table("x.xlsx", data, sheet="Q3")


class ReportAndTemplateTests(unittest.TestCase):
    def test_review_report(self):
        items = [item("B", 90, ["duplicate", "round_amount"]), item("A", 20, ["weekend_invoice"], "dismissed")]
        items[0]["vendor"] = EVIL
        wb = load_workbook(io.BytesIO(review_report(items, "Acme Inc", {"invoices": 9, "open": 1})))
        ws = wb["Review queue"]
        self.assertEqual([ws.cell(2, c).value for c in (1, 2, 6, 7, 8)],
                         [90, "B", "Needs review", "Review first", "Duplicate, Round amount"])
        self.assertEqual((ws.cell(3, 6).value, ws.cell(3, 7).value), ("False alarm", "Pay"))
        self.assertEqual(ws.cell(2, 3).data_type, "s")
        self.assertEqual(ws.auto_filter.ref, "A1:K3")

    def test_payment_file_sorts_every_invoice(self):
        invoices = [{"id": i, "vendor": "V", "amount": a, "invoice_date": "2026-01-05"}
                    for i, a in (("P1", 100.0), ("D1", 250.0), ("H1", 40.0), ("R1", 7.5), ("X1", 1.0))]
        queue = {"D1": item("D1", 90, ["duplicate"], "confirmed"),
                 "H1": {**item("H1", 80, ["bank_account_change"], "confirmed"), "note": "calling them"},
                 "R1": item("R1", 50, ["round_amount"]),
                 "X1": item("X1", 20, ["weekend_invoice"], "dismissed")}
        wb = load_workbook(io.BytesIO(payment_file(invoices, queue, "Acme Inc")))
        self.assertEqual(wb.sheetnames, ["Summary", "Ready to pay", "Review first", "On hold", "Do not pay"])
        ids = {name: [wb[name].cell(r, 1).value for r in range(2, wb[name].max_row)] for name in wb.sheetnames[1:]}
        self.assertEqual(ids, {"Ready to pay": ["P1", "X1"], "Review first": ["R1"], "On hold": ["H1"], "Do not pay": ["D1"]})
        summary = wb["Summary"]
        self.assertEqual([summary.cell(r, 3).value for r in range(5, 10)], [101.0, 7.5, 40.0, 250.0, 398.5])
        self.assertEqual(wb["On hold"].cell(2, 8).value, "calling them")
        self.assertIn("phone number", wb["On hold"].cell(2, 7).value)
        self.assertEqual(wb["Do not pay"].cell(3, 4).value, 250.0)  # total row

    def test_template_round_trips_through_the_importer(self):
        table = read_table("template.xlsx", template())
        info = preview(table)
        self.assertEqual(info["mapping"], {"id": "Invoice number", "vendor": "Vendor", "amount": "Amount",
                                           "date": "Invoice date", "bank_account": "Bank account"})
        self.assertEqual(len(convert(table, info["mapping"]).invoices), 1)


if __name__ == "__main__":
    unittest.main()
