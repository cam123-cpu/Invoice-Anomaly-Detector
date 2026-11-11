"""Synthetic invoice *documents* (PDF and Excel) with known answers.

Used three ways: the sample files on the Upload page, the tests, and the extraction
benchmark (`cli evaluate-documents`), which measures how often each field is read
correctly across many layouts, label wordings, date and number formats.

The PDF writer is deliberately tiny (standard fonts, text and filled boxes), so the app
needs no PDF-generation library. Everything here is made up.
"""
from __future__ import annotations

import functools
import io
import random
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta

PAGE_W, PAGE_H = 612, 792  # US Letter, in points


# ------------------------------------------------------------------ a minimal PDF writer
def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf_bytes(items: list[tuple]) -> bytes:
    """items: ("text", x, y_top, size, bold, text) or ("rect", x, y_top, w, h, gray 0..1).
    y is measured from the top of the page, like the layouts below."""
    ops = []
    for it in items:
        if it[0] == "rect":
            _, x, y, w, h, gray = it
            ops.append(f"{gray:.2f} g {x:.1f} {PAGE_H - y - h:.1f} {w:.1f} {h:.1f} re f 0 g")
        else:
            _, x, y, size, bold, text = it
            font = "F2" if bold else "F1"
            safe = _esc(text.encode("cp1252", "replace").decode("cp1252"))
            ops.append(f"BT /{font} {size} Tf {x:.1f} {PAGE_H - y - size:.1f} Td ({safe}) Tj ET")
    stream = "\n".join(ops).encode("cp1252", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] /Contents 4 0 R "
        f"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>".encode(),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _width(text: str, size: float) -> float:
    return len(text) * size * 0.5   # close enough to Helvetica for right-aligning numbers


# ------------------------------------------------------------------ the invoice being drawn
@dataclass
class Doc:
    vendor: str
    vendor_lines: list[str]
    email: str
    customer: str
    customer_lines: list[str]
    number: str
    issued: date
    due: date
    items: list[tuple[str, int, float]]
    tax_rate: float = 0.0
    shipping: float = 0.0
    discount: float = 0.0
    bank: str | None = None
    style: str = "classic"
    date_format: str = "%B %d, %Y"
    money_format: str = "us"         # us: 1,234.56   eu: 1.234,56
    symbol: str = "$"
    labels: dict = field(default_factory=dict)
    total_error: float = 0.0         # >0: the printed total is wrong by this much

    @property
    def subtotal(self) -> float:
        return round(sum(q * p for _, q, p in self.items), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.tax_rate, 2)

    @property
    def true_total(self) -> float:
        return round(self.subtotal - self.discount + self.tax + self.shipping, 2)

    @property
    def printed_total(self) -> float:
        return round(self.true_total + self.total_error, 2)

    def m(self, amount: float, symbol: bool = True) -> str:
        text = f"{amount:,.2f}"
        if self.money_format == "eu":
            text = text.replace(",", "X").replace(".", ",").replace("X", ".")
            return f"{text} {self.symbol}" if symbol else text
        return f"{self.symbol}{text}" if symbol else text

    def d(self, when: date) -> str:
        return when.strftime(self.date_format)

    def label(self, key: str, default: str) -> str:
        return self.labels.get(key, default)

    def truth(self) -> dict:
        return {"id": self.number, "vendor": self.vendor, "date": self.issued.isoformat(),
                "amount": self.printed_total, "bank_account": (self.bank or "").replace(" ", "") or None}


# ------------------------------------------------------------------ layouts
def layout(doc: Doc) -> list[tuple]:
    """Positioned text for one invoice, in one of four common layouts."""
    T = []

    def text(x, y, s, size=10, bold=False):
        T.append(("text", x, y, size, bold, s))

    def right(x_right, y, s, size=10, bold=False):
        text(x_right - _width(s, size), y, s, size, bold)

    def table(y):
        heads = [doc.label("desc", "Description"), doc.label("qty", "Qty"), doc.label("price", "Unit price"),
                 doc.label("line", "Amount")]
        T.append(("rect", 50, y - 4, 512, 20, 0.9))
        text(56, y, heads[0], 10, True)
        right(360, y, heads[1], 10, True)
        right(460, y, heads[2], 10, True)
        right(556, y, heads[3], 10, True)
        y += 24
        for desc, qty, price in doc.items:
            text(56, y, desc)
            right(360, y, str(qty))
            right(460, y, doc.m(price, False))
            right(556, y, doc.m(qty * price, False))
            y += 18
        return y + 10

    def totals(y, x_label=380):
        rows = [(doc.label("subtotal", "Subtotal"), doc.subtotal)]
        if doc.discount:
            rows.append((doc.label("discount", "Discount"), doc.discount))
        if doc.tax_rate:
            rows.append((doc.label("tax", "Sales tax"), doc.tax))
        if doc.shipping:
            rows.append((doc.label("shipping", "Shipping"), doc.shipping))
        for label, amount in rows:
            text(x_label, y, label)
            right(556, y, doc.m(amount))
            y += 18
        T.append(("rect", x_label - 6, y - 4, 562 - x_label, 22, 0.85))
        text(x_label, y, doc.label("total", "Total"), 11, True)
        right(556, y, doc.m(doc.printed_total), 11, True)
        return y + 36

    if doc.style == "classic":
        text(50, 50, doc.vendor, 20, True)
        for i, line in enumerate(doc.vendor_lines + [doc.email]):
            text(50, 78 + i * 14, line, 9)
        right(562, 50, "INVOICE", 24, True)
        text(380, 90, doc.label("number", "Invoice #:"), 10, True)
        right(562, 90, doc.number)
        text(380, 106, doc.label("date", "Invoice date:"), 10, True)
        right(562, 106, doc.d(doc.issued))
        text(380, 122, "Due date:", 10, True)
        right(562, 122, doc.d(doc.due))
        text(50, 160, doc.label("billto", "Bill to:"), 10, True)
        for i, line in enumerate([doc.customer] + doc.customer_lines):
            text(50, 176 + i * 14, line)
        y = totals(table(250))
        text(50, y, f"{doc.label('payable', 'Make all checks payable to')} {doc.vendor}", 9)
        if doc.bank:
            text(50, y + 14, f"{doc.label('bank', 'IBAN')}: {doc.bank}", 9)
        text(50, y + 40, "Thank you for your business!", 10, True)
    elif doc.style == "band":
        T.append(("rect", 0, 30, PAGE_W, 60, 0.15))
        text(50, 48, "INVOICE", 26, True)
        text(50, 110, doc.vendor, 16, True)
        for i, line in enumerate(doc.vendor_lines):
            text(50, 132 + i * 13, line, 9)
        heads = [(doc.label("number", "Invoice no."), doc.number), (doc.label("date", "Date"), doc.d(doc.issued)),
                 ("Due", doc.d(doc.due)), (doc.label("due_label", "Amount due"), doc.m(doc.printed_total))]
        for i, (head, value) in enumerate(heads):
            text(320 + (i % 2) * 130, 110 + (i // 2) * 36, head.upper(), 8, True)
            text(320 + (i % 2) * 130, 122 + (i // 2) * 36, value, 10)
        text(50, 200, doc.label("billto", "Billed to"), 9, True)
        for i, line in enumerate([doc.customer] + doc.customer_lines):
            text(50, 214 + i * 13, line, 10)
        y = totals(table(290))
        if doc.bank:
            text(50, y, f"{doc.label('bank', 'Bank account')}: {doc.bank}", 9)
        text(50, y + 16, doc.email, 9)
    elif doc.style == "inline":
        text(50, 50, doc.vendor, 18, True)
        text(50, 74, " · ".join(doc.vendor_lines), 9)
        text(50, 110, f"{doc.label('number', 'Invoice #')}{doc.number}", 14, True)
        text(50, 132, f"{doc.label('date', 'Issued on')} {doc.d(doc.issued)}", 10)
        text(50, 148, f"Due {doc.d(doc.due)}", 10)
        text(330, 110, doc.label("billto", "Invoice to"), 9, True)
        for i, line in enumerate([doc.customer] + doc.customer_lines):
            text(330, 124 + i * 13, line, 10)
        y = totals(table(220))
        text(50, y, f"{doc.label('due_label', 'Balance due')} {doc.m(doc.printed_total)}", 12, True)
        if doc.bank:
            text(50, y + 20, f"{doc.label('bank', 'IBAN')} {doc.bank}", 9)
        text(50, y + 34, doc.email, 9)
    else:  # "european": tax invoice, VAT, day-first dates, 1.234,56 €
        text(50, 50, doc.label("title", "TAX INVOICE"), 22, True)
        text(330, 50, doc.vendor, 14, True)
        for i, line in enumerate(doc.vendor_lines + [doc.email]):
            text(330, 70 + i * 13, line, 9)
        text(50, 140, doc.label("number", "Invoice No."), 10, True)
        text(150, 140, doc.number)
        text(50, 156, doc.label("date", "Date of issue"), 10, True)
        text(150, 156, doc.d(doc.issued))
        text(50, 186, doc.label("billto", "Customer"), 10, True)
        for i, line in enumerate([doc.customer] + doc.customer_lines):
            text(150, 186 + i * 13, line)
        y = totals(table(270))
        if doc.bank:
            text(50, y, f"{doc.label('bank', 'IBAN')}: {doc.bank}", 9)
    return T


def to_pdf(doc: Doc) -> bytes:
    return pdf_bytes(layout(doc))


def to_xlsx(doc: Doc) -> bytes:
    """The same layout as an Excel form: each piece of text in the cell nearest its spot,
    numbers stored as numbers and dates as dates, the way Excel invoice templates are."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    for col in "ABCDEFGHIJ":
        ws.column_dimensions[col].width = 12
    taken = {}
    for it in layout(doc):
        if it[0] != "text":
            continue
        _, x, y, size, bold, s = it
        row, col = int(y // 16) + 1, min(int(x // 62) + 1, 10)
        while (row, col) in taken:
            col += 1
        taken[(row, col)] = True
        value: object = s
        if s == doc.d(doc.issued):
            value = doc.issued
        elif s == doc.m(doc.printed_total) and doc.money_format == "us" and doc.symbol == "$":
            value = doc.printed_total
        cell = ws.cell(row=row, column=col, value=value)
        if isinstance(value, date):
            cell.number_format = "mmmm d, yyyy"
        elif isinstance(value, float):
            cell.number_format = '"$"#,##0.00'
        cell.font = Font(size=size, bold=bold)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ------------------------------------------------------------------ random invoices
VENDORS = [("Northwind Traders", "northwindtraders.example"), ("Bluebird Print Co.", "bluebirdprint.example"),
           ("Harbor Logistics LLC", "harborlogistics.example"), ("Maple & Finch Design", "maplefinch.example"),
           ("Copperline Electrical", "copperline.example"), ("Summit IT Services", "summitit.example"),
           ("Greenleaf Catering", "greenleaf.example"), ("Ironwood Builders Inc", "ironwood.example"),
           ("Pacific Office Supply", "pacificoffice.example"), ("Lumen Analytics Ltd", "lumenanalytics.example")]
CUSTOMERS = ["Acme Inc", "Acme Inc.", "ACME INC", "Acme Incorporated"]
STREETS = ["120 Harbor Way", "45 Elm Street", "8 Market Square", "2200 Industrial Pkwy", "77 King Road"]
CITIES = ["Springfield, IL 62701", "Portland, OR 97205", "Austin, TX 78701", "Leeds LS1 4AP", "Denver, CO 80202"]
ITEMS = ["Consulting hours", "Printer toner", "Pallet delivery", "Design revisions", "Cable install",
         "Server maintenance", "Lunch catering", "Framing labour", "Copy paper (box)", "Data report"]
IBANS = ["GB29NWBK60161331926819", "DE89370400440532013000", "FR1420041010050500013M02606",
         "NL91ABNA0417164300", "GB82WEST12345698765432", "IE29AIBK93115212345678"]
LABEL_VARIANTS = {
    "number": {"classic": ["Invoice #:", "Invoice No:", "Invoice Number:", "INVOICE #"],
               "band": ["Invoice no.", "Invoice number", "Invoice #"],
               "inline": ["Invoice #", "Invoice No. ", "Invoice number "],
               "european": ["Invoice No.", "Invoice number", "Invoice ref"]},
    "date": {"classic": ["Invoice date:", "Date:", "Date issued:", "Issue date:"],
             "band": ["Date", "Invoice date", "Issued"], "inline": ["Issued on", "Invoice date", "Dated"],
             "european": ["Date of issue", "Invoice date", "Tax point"]},
    "total": {"classic": ["Total", "TOTAL", "Total due", "Amount due"], "band": ["Total", "Total due"],
              "inline": ["Total", "Grand total"], "european": ["Total incl. VAT", "Total", "Amount payable"]},
    "billto": {"classic": ["Bill to:", "Billed to:", "Sold to:"], "band": ["Billed to", "Bill to"],
               "inline": ["Invoice to", "Bill to"], "european": ["Customer", "Invoice to"]},
}


def random_doc(rng: random.Random, style: str | None = None) -> Doc:
    style = style or rng.choice(["classic", "band", "inline", "european"])
    vendor, domain = rng.choice(VENDORS)
    issued = date(2026, 1, 2) + timedelta(days=rng.randrange(300))
    eu = style == "european"
    items = [(rng.choice(ITEMS), rng.randint(1, 12), round(rng.uniform(8, 900), 2)) for _ in range(rng.randint(1, 6))]
    number = rng.choice([f"INV-{rng.randint(1, 9999):04d}", str(rng.randint(100, 99999)),
                         f"{issued.year}-{rng.randint(1, 999):03d}", f"A{rng.randint(10, 99)}-{rng.randint(100, 999)}"])
    if eu:
        date_format = rng.choice(["%d/%m/%Y", "%d.%m.%Y", "%d %B %Y"])
        if date_format != "%d %B %Y" and issued.day <= 12:   # keep day-first dates unambiguous
            issued = issued.replace(day=rng.randint(13, 28))
    else:
        date_format = rng.choice(["%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%d %b %Y"])
    labels = {k: rng.choice(v[style]) for k, v in LABEL_VARIANTS.items()}
    if eu:
        labels["tax"] = "VAT 20%"
    return Doc(
        vendor=vendor, vendor_lines=[rng.choice(STREETS), rng.choice(CITIES)], email=f"billing@{domain}",
        customer=rng.choice(CUSTOMERS), customer_lines=["Accounts Payable", rng.choice(STREETS)],
        number=number, issued=issued, due=issued + timedelta(days=30), items=items,
        tax_rate=0.2 if eu else rng.choice([0.0, 0.0625, 0.08, 0.0725]),
        shipping=rng.choice([0.0, 0.0, 25.0, 49.5]), discount=rng.choice([0.0, 0.0, 0.0, 10.0]),
        bank=rng.choice(IBANS) if (eu or rng.random() < 0.5) else None, style=style, date_format=date_format,
        money_format="eu" if eu else "us", symbol="€" if eu else rng.choice(["$", "$", "USD "]), labels=labels,
        total_error=rng.choice([0.0] * 9 + [round(rng.uniform(20, 400), 2)]))


def random_docs(n: int, seed: int = 7) -> list[tuple[str, bytes, Doc]]:
    """n invoices, alternating PDF and Excel, as (filename, bytes, doc)."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        doc = random_doc(rng)
        if i % 2:
            out.append((f"invoice-{i:03d}.xlsx", to_xlsx(doc), doc))
        else:
            out.append((f"invoice-{i:03d}.pdf", to_pdf(doc), doc))
    return out


# ------------------------------------------------------------------ the sample files on the Upload page
@functools.lru_cache(maxsize=1)
def sample_docs() -> list[tuple[str, bytes, Doc]]:
    """A few invoices from vendors in the demo data, each showing something:
    a normal invoice, one paid into a new bank account, one whose totals don't add up,
    and one from a look-alike vendor name."""
    base = dict(vendor_lines=["2200 Industrial Pkwy", "Austin, TX 78701"], customer="Acme Inc",
                customer_lines=["Accounts Payable", "45 Elm Street"])
    docs = [
        ("wayne-office-1042.pdf", Doc(vendor="Wayne Office", email="billing@wayneoffice.example",
                                           number="WO-1042", issued=date(2026, 10, 6), due=date(2026, 11, 5),
                                           items=[("Copy paper (box)", 4, 24.5), ("Printer toner", 1, 61.0)],
                                           tax_rate=0.0625, style="classic", **base), "pdf"),
        ("stark-freight-8811.pdf", Doc(vendor="Stark Freight", email="accounts@starkfreight.example", number="SF-8811",
                                       issued=date(2026, 10, 14), due=date(2026, 11, 13),
                                       items=[("Pallet delivery", 6, 790.0), ("Fuel surcharge", 1, 412.0)],
                                       bank="GB29 NWBK 6016 1331 9268 19", style="band", **base), "pdf"),
        ("umbrella-ltd-3307.xlsx", Doc(vendor="Umbrella Ltd", email="ar@umbrella.example", number="3307",
                                       issued=date(2026, 10, 20), due=date(2026, 11, 19),
                                       items=[("Consulting hours", 6, 115.0), ("Data report", 1, 140.0)],
                                       tax_rate=0.08, total_error=100.0, style="classic", **base), "xlsx"),
        ("acme-suplies-0091.pdf", Doc(vendor="Acme Suplies", email="sales@acme-suplies.example", number="0091",
                                      issued=date(2026, 10, 22), due=date(2026, 11, 21),
                                      items=[("Office chairs", 3, 189.0)], style="inline", **base), "pdf"),
    ]
    return [(name, to_pdf(doc) if kind == "pdf" else to_xlsx(doc), doc) for name, doc, kind in docs]


def sample_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data, _ in sample_docs():
            z.writestr(name, data)
    return out.getvalue()


# ------------------------------------------------------------------ benchmark
def evaluate_documents(n: int = 100, seed: int = 7, company: str = "Acme Inc") -> dict:
    """Read n generated invoices (half PDF, half Excel; four layouts; many wordings and
    formats) and score each field against the known answer. Also scores the arithmetic
    check: planted wrong totals caught, and false alarms on correct invoices."""
    from .documents import norm, read_document
    fields = ("id", "vendor", "date", "amount", "bank_account")
    right = dict.fromkeys(fields, 0)
    total = dict.fromkeys(fields, 0)
    caught = missed = false_alarms = 0
    mistakes = []
    for name, data, doc in random_docs(n, seed):
        ex = read_document(name, data, company)
        truth = doc.truth()
        for f in fields:
            if truth[f] is None:
                continue
            total[f] += 1
            got = ex.fields.get(f)
            ok = (got is not None and abs(got - truth[f]) < 0.005) if f == "amount" else \
                norm(str(got or "")).rstrip(".") == norm(str(truth[f])).rstrip(".")
            right[f] += ok
            if not ok:
                mistakes.append({"file": name, "layout": doc.style, "field": f, "expected": truth[f], "read": got})
        flagged = any("add up" in w for w in ex.warnings)
        if doc.total_error:
            caught += flagged
            missed += not flagged
        else:
            false_alarms += flagged
    return {"documents": n,
            "accuracy": {f: round(right[f] / total[f], 4) if total[f] else None for f in fields},
            "totals_check": {"caught": caught, "missed": missed, "false_alarms": false_alarms},
            "mistakes": mistakes[:20]}
