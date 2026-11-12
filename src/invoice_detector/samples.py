"""Sample Excel files in the shapes real accounting exports take, built from the synthetic
demo data (with its planted problems). Used by the web app's "Try a sample" buttons, the
tests, and scripts/make_samples.py, which writes copies to samples/ in the repository.

Nothing here is real data.
"""
import io
from datetime import date, datetime

from openpyxl import Workbook
from openpyxl.styles import Font

from .demo import generate_demo
from .excel_out import template

FIXED_TIME = datetime(2026, 1, 1)


def _rename(rows, names, id_fmt):
    out = []
    for i, r in enumerate(rows, start=1):
        # Credit notes appear the way accounting systems export them: a negative amount.
        credit = r.get("kind") == "credit"
        out.append({**r, "id": id_fmt(i), "vendor": names.get(r["vendor"], r["vendor"]),
                    "date": date.fromisoformat(r["date"]), "amount": -r["amount"] if credit else r["amount"],
                    "bank_account": r["bank_account"] or "", "credit": credit})
    return out


def _save(wb) -> bytes:
    wb.properties.created = wb.properties.modified = FIXED_TIME
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def quickbooks() -> bytes:
    """QuickBooks 'Unpaid Bills Detail' style: report title rows, real Excel dates in US
    format, 'Num' and 'Name' columns, a totals row, and no bank details."""
    rows, _ = generate_demo(seed=1)
    rows = _rename(rows, {"Acme Supplies": "Bayside Office Supply", "Acme Suppiles": "Bayside Ofice Supply",
                          "Globex Corp": "Pacific Freight Co", "Initech LLC": "Summit IT Services",
                          "Umbrella Ltd": "Redwood Facilities", "Hooli Cloud": "CloudNest Hosting",
                          "Stark Freight": "Golden Gate Logistics", "Wayne Office": "Mission Print & Copy",
                          "Wonka Catering": "Harbor Catering", "Vandelay Industries": "Vandelay Imports"},
                   lambda i: str(1000 + i))
    wb = Workbook()
    ws = wb.active
    ws.title = "Unpaid Bills Detail"
    for line in ("Northwind Demo Company", "Unpaid Bills Detail", "All Dates", None):
        ws.append([line])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Date", "Transaction Type", "Num", "Name", "Due Date", "Amount", "Open Balance"])
    for c in ws[5]:
        c.font = Font(bold=True)
    for r in rows:
        due = date.fromordinal(r["date"].toordinal() + 30)
        ws.append([r["date"], "Vendor Credit" if r["credit"] else "Bill", r["id"], r["vendor"], due, r["amount"],
                   r["amount"]])
    total = round(sum(r["amount"] for r in rows), 2)
    ws.append([None, None, None, "TOTAL", None, total, total])
    for row in ws.iter_rows(min_row=6):
        row[0].number_format = row[4].number_format = "mm/dd/yyyy"
        row[5].number_format = row[6].number_format = "#,##0.00"
    for col, width in zip("ABCDEFG", (12, 16, 8, 28, 12, 12, 12)):
        ws.column_dimensions[col].width = width
    return _save(wb)


def eu_ledger() -> bytes:
    """A European supplier ledger: dates typed as text day-first (31/03/2026), amounts as
    text with comma decimals (1.234,56), IBANs, and net / VAT / gross columns."""
    rows, _ = generate_demo(seed=2)
    rows = _rename(rows, {"Acme Supplies": "Müller Bürobedarf GmbH", "Acme Suppiles": "Müller Bürobedraf GmbH",
                          "Globex Corp": "Lumière Transport SARL", "Initech LLC": "Datawerk IT AG",
                          "Umbrella Ltd": "Van Dijk Facilitair BV", "Hooli Cloud": "NordCloud Oy",
                          "Stark Freight": "Rossi Spedizioni SpA", "Wayne Office": "Papelería Ortega SL",
                          "Wonka Catering": "Café Lindqvist AB", "Vandelay Industries": "Vandelay Europe Ltd"},
                   lambda i: f"RE-2026-{i:04d}")

    def eu(amount: float) -> str:
        whole, cents = f"{amount:,.2f}".split(".")
        return whole.replace(",", ".") + "," + cents

    wb = Workbook()
    ws = wb.active
    ws.title = "Supplier ledger"
    ws.append(["Invoice No.", "Supplier", "Invoice date", "Net (EUR)", "VAT (EUR)", "Gross amount (EUR)",
               "IBAN", "Cost centre"])
    for c in ws[1]:
        c.font = Font(bold=True)
    centres = ["CC-100 Admin", "CC-200 Ops", "CC-300 IT"]
    for i, r in enumerate(rows):
        net = round(r["amount"] / 1.2, 2)
        iban = "DE" + "".join(ch for ch in r["bank_account"] if ch.isdigit()).ljust(20, "0")[:20]
        ws.append([r["id"], r["vendor"], r["date"].strftime("%d/%m/%Y"), net, round(r["amount"] - net, 2),
                   eu(r["amount"]), iban, centres[i % 3]])
    for col, width in zip("ABCDEFGH", (16, 28, 13, 11, 11, 18, 26, 14)):
        ws.column_dimensions[col].width = width
    return _save(wb)


def ap_register() -> bytes:
    """A hand-kept AP register: one sheet per quarter plus a notes sheet, a title row,
    a mix of number and '$1,234.50' text amounts, blank lines, and a few bad rows."""
    rows, _ = generate_demo(seed=3)
    rows = _rename(rows, {"Acme Supplies": "Lakeside Stationers", "Acme Suppiles": "Lakeside Stationer",
                          "Globex Corp": "Prairie Haulage", "Initech LLC": "Northstar Software",
                          "Umbrella Ltd": "Cedar Cleaning Services", "Hooli Cloud": "Skyline SaaS",
                          "Stark Freight": "Ironhorse Freight", "Wayne Office": "Copper Desk Supplies",
                          "Wonka Catering": "Maple Leaf Catering", "Vandelay Industries": "Vandelay Trading"},
                   lambda i: f"AP-{i:04d}")
    wb = Workbook()
    notes = wb.active
    notes.title = "Notes"
    for line in ("Accounts payable register, kept by the finance team.",
                 "One sheet per quarter. Amounts are what the supplier billed, tax included.",
                 "Rows marked TBC are waiting for the invoice copy."):
        notes.append([line])
    notes.column_dimensions["A"].width = 80
    quarters = {"Q1 2026": (1, 3), "Q2 2026": (4, 6), "Q3 2026": (7, 9), "Q4 2026": (10, 12)}
    for name, (first, last) in quarters.items():
        part = [r for r in rows if first <= r["date"].month <= last]
        if not part:
            continue
        ws = wb.create_sheet(name)
        ws.append([f"AP register - {name}"])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append(["Ref", "Payee", "Bill date", "Amount due", "Bank account", "Approved by"])
        for c in ws[2]:
            c.font = Font(bold=True)
        for i, r in enumerate(part):
            amount = f"${r['amount']:,.2f}".replace("$-", "-$") if i % 3 == 0 else r["amount"]
            ws.append([r["id"], r["vendor"], r["date"], amount, r["bank_account"], ["J. Ortiz", "M. Chen"][i % 2]])
            if i % 25 == 24:
                ws.append([])
        ws.append([f"{part[0]['id']}-X", "Pending supplier", part[0]["date"], "TBC", None, None])
        for row in ws.iter_rows(min_row=3):
            row[2].number_format = "yyyy-mm-dd"
        for col, width in zip("ABCDEF", (10, 26, 12, 13, 22, 12)):
            ws.column_dimensions[col].width = width
    return _save(wb)


SAMPLES = {
    "quickbooks-unpaid-bills.xlsx": ("QuickBooks 'Unpaid bills' export", "US dates, report title rows, a totals row, no bank details", quickbooks),
    "european-supplier-ledger.xlsx": ("European supplier ledger", "Day-first dates and 1.234,56 amounts typed as text, IBANs", eu_ledger),
    "ap-register-2026.xlsx": ("Hand-kept AP register", "One sheet per quarter, mixed amount formats, blank lines and a bad row", ap_register),
    "invoice-template.xlsx": ("Blank template", "The columns the app needs, with one example row", template),
}


def build(name: str) -> bytes:
    if name not in SAMPLES:
        raise KeyError(name)
    return SAMPLES[name][2]()
