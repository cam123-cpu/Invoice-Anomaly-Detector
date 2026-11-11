"""Excel invoices laid out like five popular free invoice templates (Microsoft's "Sales
invoice", "Small business sales invoice", "Simple business invoice", "Service invoice" and
"Simple invoice"), with the sample text those templates ship with. The extractor never
saw these while it was being written; they're here to keep it honest.

Each builder returns (xlsx bytes, the answers a person would give)."""
import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Font


def _book(cells, widths=None):
    wb = Workbook()
    ws = wb.active
    for col, w in (widths or {}).items():
        ws.column_dimensions[col].width = w
    for ref, value, *style in cells:
        ws[ref] = value
        if style:
            size, bold = style[0], (style[1] if len(style) > 1 else False)
            ws[ref].font = Font(size=size, bold=bold)
        if isinstance(value, date):
            ws[ref].number_format = "m/d/yyyy"
        elif isinstance(value, float):
            ws[ref].number_format = '"$"#,##0.00'
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def sales_invoice():
    """Shadow Beauty: label cells left of values, two header rows of small tables."""
    lines = [(55, "0402", "Liquid foundation", 2.75, 0.0), (30, "6235", "Eyeshadow palette", 1.24, 0.0),
             (16, "428", "Lipstick", 0.79, 0.0), (18, "3582", "Makeup brush set", 3.53, 6.64)]
    cells = [("A1", "INVOICE", 20, True), ("A3", "SHADOW BEAUTY", 14, True), ("E3", "Make all checks payable to"),
             ("A4", "Be uniquely you", 9), ("E4", "Street Address, City, State, Zip Code"),
             ("A6", "TO"), ("B6", "Ann Montesano"), ("E6", "DATE"), ("F6", date(2023, 6, 30)),
             ("B7", "Contoso Beauty"), ("A9", "CUSTOMER ID"), ("B9", "065065"), ("E9", "SHIP TO"),
             ("F9", "123 Sycamore Ave, Albany, NY 30001"), ("A10", "INVOICE #"), ("B10", "5585"),
             ("E10", "PHONE NUMBER"), ("F10", "555-123-0101")]
    heads = ["SALESPERSON", "JOB", "SHIPPING METHOD", "DELIVERY DATE", "PAYMENT TERMS", "DUE DATE"]
    vals = ["Kendall Collins", "Sales", "As express", date(2023, 6, 28), "On Delivery", date(2023, 7, 30)]
    for i, (h, v) in enumerate(zip(heads, vals)):
        cells += [(f"{'ABCDEF'[i]}12", h, 10, True), (f"{'ABCDEF'[i]}13", v)]
    for i, h in enumerate(["QTY", "ITEM#", "DESCRIPTION", "UNIT PRICE", "DISCOUNT", "LINE TOTAL"]):
        cells.append((f"{'ABCDEF'[i]}15", h, 10, True))
    subtotal = 0.0
    for r, (q, item, desc, price, disc) in enumerate(lines, 16):
        line = round(q * price - disc, 2)
        subtotal += line
        cells += [(f"A{r}", q), (f"B{r}", item), (f"C{r}", desc), (f"D{r}", price), (f"E{r}", disc), (f"F{r}", line)]
    subtotal = round(subtotal, 2)
    tax = round(subtotal * 0.001, 2)
    total = round(subtotal + tax, 2)
    cells += [("E21", "TOTAL DISCOUNT"), ("F21", 6.64), ("E22", "SUBTOTAL"), ("F22", subtotal),
              ("E23", "SALES TAX"), ("F23", tax), ("E24", "TOTAL", 11, True), ("F24", total),
              ("A26", "THANK YOU FOR YOUR BUSINESS", 11, True)]
    return _book(cells, {c: 16 for c in "ABCDEF"}), {
        "id": "5585", "vendor": "SHADOW BEAUTY", "date": "2023-06-30", "amount": total}


def small_business_sales_invoice():
    """Pink template: logo box, then BILL TO / INFO / CONTACT columns of label + value."""
    items = [(2, "Bamboo sheet set", 89.0), (4, "Linen pillowcase", 22.5), (1, "Weighted blanket", 149.0)]
    cells = [("A1", "YOUR LOGO", 10), ("F1", "INVOICE", 26, True), ("A3", "Tailspin Linens", 13, True),
             ("A4", "4567 Main St, Buffalo, NY 98052", 9),
             ("A6", "BILL TO", 10, True), ("C6", "INFO", 10, True), ("E6", "CONTACT", 10, True),
             ("A7", "Lucas Grey"), ("C7", "Date"), ("D7", "5/4/2024"), ("E7", "Name"), ("F7", "Mia Chen"),
             ("A8", "Fourth Coffee"), ("C8", "Invoice No."), ("D8", "34"), ("E8", "Phone"), ("F8", "(206) 555-0187"),
             ("A9", "21 Pike Street"), ("C9", "Customer ID"), ("D9", "ABC12345"), ("E9", "Email"),
             ("F9", "mia@tailspinlinens.example"), ("C10", "Payment terms"), ("D10", "Net 30"),
             ("C11", "Due Date"), ("D11", "6/3/2024")]
    for i, h in enumerate(["QUANTITY", "DESCRIPTION", "UNIT PRICE", "AMOUNT", "DISCOUNT APPLIED"]):
        cells.append((f"{'ABCDE'[i]}13", h, 10, True))
    sub = 0.0
    for r, (q, d, p) in enumerate(items, 14):
        sub += q * p
        cells += [(f"A{r}", q), (f"B{r}", d), (f"C{r}", p), (f"D{r}", round(q * p, 2)), (f"E{r}", "No")]
    sub = round(sub, 2)
    tax = round(sub * 0.088, 2)
    cells += [("C19", "Subtotal"), ("D19", sub), ("C20", "Sales Tax"), ("D20", tax),
              ("C21", "Total", 11, True), ("D21", round(sub + tax, 2))]
    return _book(cells, {c: 15 for c in "ABCDEF"}), {
        "id": "34", "vendor": "Tailspin Linens", "date": "2024-05-04", "amount": round(sub + tax, 2)}


def simple_business_invoice():
    """HLP Manufacturing: "DATE:" / "INVOICE #" on the right, "$" in its own column."""
    rows = [("Custom machine parts", 2250.0), ("Injection mold tools", 1250.0), ("Steel alloy sheets", 3000.0),
            ("Machining services", 4500.0), ("Quality inspection", 250.0)]
    cells = [("A1", "INVOICE", 22, True), ("A3", "HLP Manufacturing INC", 12, True), ("A4", "123 40th Street"),
             ("A5", "Chicago, IL 54321"), ("A6", "(312) 555-0113"), ("A7", "hlpmanufacturing@example.com"),
             ("D3", "DATE:", 10, True), ("E3", "November 9, 2023"), ("D4", "INVOICE #", 10, True), ("E4", 100),
             ("D5", "BILL TO:", 10, True), ("E5", "Jodi Kim"), ("E6", "Fabrikam Manufacturing"),
             ("E7", "89 Pacific Ave"), ("E8", "San Francisco, CA 45432"), ("E9", "(312) 555-0157"),
             ("A11", "CUSTOM MACHINE PARTS MANUFACTURING", 12), ("A13", "DESCRIPTION", 10, True),
             ("E13", "AMOUNT", 10, True)]
    for r, (d, amount) in enumerate(rows, 14):
        cells += [(f"A{r}", d), (f"D{r}", "$"), (f"E{r}", amount)]
    total = sum(a for _, a in rows)
    cells += [("C22", "SUBTOTAL"), ("D22", "$"), ("E22", total), ("C23", "TAX RATE"), ("E23", "0.00%"),
              ("C24", "SALES TAX"), ("D24", "$"), ("E24", "-"), ("C25", "OTHER"), ("D25", "$"), ("E25", "-"),
              ("C26", "TOTAL", 11, True), ("D26", "$"), ("E26", total),
              ("A28", "Make all checks payable to HLP Manufacturing INC.", 9),
              ("A29", "If you have any questions concerning this invoice, contact Angela at (312) 555-0113", 9),
              ("A31", "THANK YOU FOR YOUR BUSINESS!", 10, True)]
    return _book(cells, {"A": 30, "B": 10, "C": 12, "D": 6, "E": 16}), {
        "id": "100", "vendor": "HLP Manufacturing INC", "date": "2023-11-09", "amount": total}


def service_invoice():
    """The Happy Tooth: "Invoice #5465" with the date under it and no "Date" label;
    checks payable to a person, not the business."""
    items = [(1, "Mouth mirror", 0.53), (6, "Dental cotton rolls", 6.12), (100, "Curette", 0.15),
             (12, "Syringes", 1.55), (20, "Periodontal probe", 0.9)]
    cells = [("A1", "THE HAPPY TOOTH", 18, True), ("A2", "Make all checks payable to Lilu Linna", 9),
             ("E1", "Invoice #5465", 14, True), ("E2", date(2023, 6, 8)),
             ("A4", "TO", 9, True), ("B4", "CUSTOMER ID", 9, True), ("C4", "ADDRESS", 9, True), ("D4", "PHONE", 9, True),
             ("A5", "Ana Ivanovic"), ("B5", "AB1234"), ("C5", "234 Elm St, Madison, WI 12345"), ("D5", "555-0101"),
             ("A6", "Kendall Collins Dental"), ("A8", "SALESPERSON", 9, True), ("B8", "JOB", 9, True),
             ("C8", "PAYMENT TERMS", 9, True), ("D8", "DUE DATE", 9, True), ("A9", "Jane"), ("B9", "Dental supplies"),
             ("C9", "Due upon receipt")]
    for i, h in enumerate(["QTY", "DESCRIPTION", "UNIT PRICE", "LINE TOTAL"]):
        cells.append((f"{'ABCD'[i]}11", h, 9, True))
    sub = 0.0
    for r, (q, d, p) in enumerate(items, 12):
        sub += round(q * p, 2)
        cells += [(f"A{r}", q), (f"B{r}", d), (f"C{r}", p), (f"D{r}", round(q * p, 2))]
    sub = round(sub, 2)
    tax = round(sub * 0.05, 2)
    cells += [("C18", "SUBTOTAL"), ("D18", sub), ("C19", "SALES TAX"), ("D19", tax),
              ("C20", "TOTAL", 11, True), ("D20", round(sub + tax, 2)), ("A22", "THANK YOU", 14, True),
              ("A23", "206-555-0154 | happytooth@example.com", 9)]
    return _book(cells, {"A": 22, "B": 22, "C": 26, "D": 14, "E": 18}), {
        "id": "5465", "vendor": "THE HAPPY TOOTH", "date": "2023-06-08", "amount": round(sub + tax, 2)}


def simple_invoice():
    """Elegant Embrace: small "Invoice #" block on the right, "Invoice Subtotal",
    a deposit already received, and "Please make all checks payable to ..." at the bottom."""
    items = [("A876", "Roses", 25, 1.52), ("8394", "Tulips", 28, 2.0), ("V823", "Buttercups", 10, 3.25)]
    cells = [("A1", "Elegant Embrace", 16, True), ("E1", "INVOICE", 20, True), ("E2", "Invoice #"), ("F2", "100100"),
             ("E3", "Invoice date"), ("F3", date(2023, 6, 5)), ("E4", "Due date"), ("F4", date(2023, 7, 5)),
             ("A3", "301 Ocean Ave"), ("A4", "Los Angeles, CA 34521"), ("A5", "(555) 555-1234"),
             ("A6", "elegantembrace@example.com"), ("E6", "Bill to:"), ("F6", "Haley Clark"),
             ("E7", "Address:"), ("F7", "125 Avenue A"), ("E8", "Phone:"), ("F8", "555-555-0177")]
    for i, h in enumerate(["Item #", "Description", "Qty", "Unit price", "Discount", "Price"]):
        cells.append((f"{'ABCDEF'[i]}10", h, 10, True))
    sub = 0.0
    for r, (code, d, q, p) in enumerate(items, 11):
        sub += round(q * p, 2)
        cells += [(f"A{r}", code), (f"B{r}", d), (f"C{r}", q), (f"D{r}", p), (f"F{r}", round(q * p, 2))]
    sub = round(sub, 2)
    tax = round(sub * 0.1, 2)
    deposit = 50.0
    total = round(sub + tax - deposit, 2)
    cells += [("E16", "Invoice Subtotal"), ("F16", sub), ("E17", "Tax Rate"), ("F17", "10.00%"),
              ("E18", "Sales Tax"), ("F18", tax), ("E19", "Other"), ("E20", "Deposit Received"), ("F20", deposit),
              ("E21", "TOTAL", 11, True), ("F21", total),
              ("A23", "Please make all checks payable to Elegant Embrace.", 9),
              ("A24", "Total due in 30 days. Overdue accounts subject to a service charge of 1.5% per month.", 9)]
    return _book(cells, {"A": 14, "B": 18, "C": 8, "D": 12, "E": 18, "F": 14}), {
        "id": "100100", "vendor": "Elegant Embrace", "date": "2023-06-05", "amount": total}


TEMPLATES = {"Sales invoice": sales_invoice, "Small business sales invoice": small_business_sales_invoice,
             "Simple business invoice": simple_business_invoice, "Service invoice": service_invoice,
             "Simple invoice": simple_invoice}
