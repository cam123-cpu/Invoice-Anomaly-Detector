"""Read invoice *documents* (one invoice per file) into invoice fields.

An export is a table: one invoice per row. An invoice document is a form: "Invoice #" next
to "5465", the vendor's name at the top, line items in the middle, "TOTAL" near the bottom.
This module turns any of these into the same thing, a list of positioned text boxes, and
then reads the fields from the layout:

    Excel invoice  ─┐
    PDF with text  ─┼─>  [Box(text, x0, y0, x1, y1, page, size)]  ─>  find_fields()  ─>  Extraction
    scan / photo   ─┘        (OCR, optional)

find_fields() works the way a person reads a form: find a label ("Invoice date", "Amount
due"), then take the value written after it, to its right, or under it. Every field gets a
confidence, and the totals are cross-checked (line items -> subtotal -> + tax -> total).

Everything here runs locally. Nothing is sent anywhere; ai_extract.py is the separate,
optional step that asks Claude, and only when a company has switched it on.
"""
from __future__ import annotations

import contextvars
import io
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from difflib import SequenceMatcher

from .importer import ID_PATTERN, FileProblem, MAX_BYTES, _check_xlsx, parse_date

MAX_PAGES = 5            # an invoice longer than this is almost certainly something else
MAX_CELLS = 20_000       # Excel: enough for any invoice form
MAX_IMAGE_PIXELS = 40_000_000
MIN_PDF_TEXT = 20        # fewer characters than this on a PDF page: it's a scan

FIELDS = ("id", "vendor", "date", "amount", "bank_account")
REQUIRED = ("id", "vendor", "date", "amount")


# ------------------------------------------------------------------ data model
@dataclass
class Box:
    """A piece of text and where it sits. y grows downwards; units depend on the source
    (points for PDF, a fixed grid for Excel, pixels for images) but are consistent within
    one document, which is all the layout rules need."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int = 0
    size: float = 0.0          # font size when known: titles and vendor names are bigger
    raw: object = None         # Excel: the cell's own value (a real date or number)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class Extraction:
    fields: dict = field(default_factory=dict)        # id, vendor, date, amount, bank_account
    confidence: dict = field(default_factory=dict)    # field -> 0..1
    source: dict = field(default_factory=dict)        # field -> the text it was read from
    totals: dict = field(default_factory=dict)        # subtotal, tax, shipping, discount, lines
    customer: str | None = None
    currency: str | None = None
    warnings: list = field(default_factory=list)
    method: str = ""
    pages: int = 1
    kind: str = "invoice"        # or "credit" when the document is a credit note

    @property
    def missing(self) -> list[str]:
        return [f for f in REQUIRED if not self.fields.get(f)]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["missing"] = self.missing
        return d


# ------------------------------------------------------------------ text helpers
def norm(text: str) -> str:
    """Lower case, accents removed, punctuation except # % & turned into spaces."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9#%&@.$€£ ]+", " ", text.lower().replace("nº", "no").replace("n°", "no"))
    return re.sub(r"\s+", " ", text).strip(" .")


_MONEY = re.compile(r"^[^\d]{0,4}\(?-?\s*\d[\d,.'  ]*\)?\s*(?:[A-Za-z$€£¥]{1,3})?$")
_CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_CURRENCY_CODES = ("USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY", "INR", "NZD", "SEK", "NOK", "DKK")


# On a credit note the amounts are often printed negative (-120.00 or (120.00)). While one is
# being read this is switched on, and such amounts are taken at their size.
_CREDIT_NOTE = contextvars.ContextVar("credit_note", default=False)


def money(value) -> float | None:
    """A non-negative amount, or None. Accepts zero (unlike the importer), since a tax or
    discount line is often 0.00."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value < 0 and _CREDIT_NOTE.get():
            value = -value
        return round(float(value), 2) if value >= 0 else None
    text = str(value or "").strip()
    if not text or "%" in text or not _MONEY.match(text) or not re.search(r"\d", text):
        return None
    if re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{1,4}", text):  # a date, not money
        return None
    body = re.sub(r"[^\d,.\-()'  ]", "", text).strip()
    negative = body.startswith(("-", "(")) or body.endswith(")")
    body = body.strip("()- ").replace(" ", "").replace(" ", "").replace("'", "")
    if "," in body and "." in body:
        decimal = "," if body.rfind(",") > body.rfind(".") else "."
        body = body.replace("." if decimal == "," else ",", "").replace(",", ".")
    elif "," in body:
        body = body.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", body) else body.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", body):
        body = body.replace(".", "")
    try:
        amount = float(body)
    except ValueError:
        return None
    if (negative and not _CREDIT_NOTE.get()) or amount >= 1e12:
        return None
    return round(amount, 2)


def currency_of(text: str) -> str | None:
    for sign, code in _CURRENCY_SIGNS.items():
        if sign in text:
            return code
    m = re.search(r"\b(" + "|".join(_CURRENCY_CODES) + r")\b", text.upper())
    return m.group(1) if m else None


def as_date(value, text: str) -> tuple[str | None, bool]:
    """(ISO date or None, ambiguous?). 03/04/2026 could be March or April: it's read
    month-first (US style) and reported as ambiguous so a person checks it."""
    if isinstance(value, datetime):
        return value.date().isoformat(), False
    if isinstance(value, date):
        return value.isoformat(), False
    text = re.sub(r"(?i)\b(\d{1,2})(st|nd|rd|th)\b", r"\1", str(text or "")).strip(" ,.")
    text = re.sub(r"(?i)^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+", "", text)
    text = re.sub(r"\s+", " ", text)
    mdy = dmy = None
    for order in ("MDY", "DMY"):
        try:
            iso = parse_date(text, order)
        except ValueError:
            continue
        if order == "MDY":
            mdy = iso
        else:
            dmy = iso
    if mdy and dmy and mdy != dmy:
        return mdy, True
    return mdy or dmy, False


_ADDRESS = re.compile(
    r"(?i)(^\d+\s+\w+)|\b(street|st\.?|avenue|ave\.?|road|rd\.?|suite|ste\.?|blvd|boulevard|lane|ln\.?|"
    r"drive|dr\.?|floor|p\.?o\.? box|city|state|zip|postcode|address)\b|\b\d{5}(-\d{4})?\b|"
    r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b|,\s*[A-Z]{2}\s+\d{5}")
_CONTACT = re.compile(r"(?i)@|www\.|https?://|\.com\b|\.co\b|\.org\b|\.net\b|^\(?\+?\d[\d\s().-]{6,}$|"
                      r"\b(tel|phone|fax|email|e-mail|mobile)\b")
_NOT_A_NAME = {"credit note", "credit memo", "tax credit note", "credit invoice", "credit", "invoice", "tax invoice", "commercial invoice", "sales invoice", "proforma invoice",
               "your logo", "logo", "company logo", "your company", "company name", "thank you",
               "thank you for your business", "bill", "receipt", "statement", "original", "copy",
               "page", "description", "qty", "quantity", "unit price", "amount", "total", "notes",
               "item", "item #", "items", "item no", "price", "discount", "line total", "rate", "hours",
               "unit", "unit cost", "product", "service", "details", "sku", "code"}


def looks_like_name(text: str) -> bool:
    t = text.strip()
    n = norm(t)
    if len(n) < 2 or len(t) > 80 or n in _NOT_A_NAME or not re.search(r"[A-Za-z]{2}", t):
        return False
    if _ADDRESS.search(t) or _CONTACT.search(t) or money(t) is not None or as_date(None, t)[0]:
        return False
    if re.search(r"(?i)\b(invoice|credit note|credit memo|page \d|thank|logo|payable|due|terms)\b", t):
        return False
    return sum(c.isdigit() for c in t) <= len(t) // 3


def similar(a: str, b: str) -> float:
    a, b = norm(a), norm(b)
    for suffix in (" inc", " llc", " ltd", " limited", " corp", " co", " gmbh", " plc", " sa"):
        a, b = a.removesuffix(suffix), b.removesuffix(suffix)
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


# ------------------------------------------------------------------ IBAN and accounts
def iban_ok(text: str) -> bool:
    s = re.sub(r"\s+", "", text).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1   # the IBAN checksum: catches typos and OCR misreads


def account_value(text: str) -> str | None:
    t = text.strip().strip(":#").strip()
    m = re.search(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{2,4}){3,8})\b", t.upper())
    if m and iban_ok(m.group(1)):
        return re.sub(r"\s+", "", m.group(1))
    m = re.fullmatch(r"(?i)(?:acct\.?|account|no\.?|#)?\s*([A-Z]{0,4}[\d][\d -]{4,30}\d)", t)
    if m:
        value = re.sub(r"[ -]", "", m.group(1))
        if 6 <= len(value) <= 34 and sum(c.isdigit() for c in value) >= 6:
            return value
    return None


# ------------------------------------------------------------------ labels
# (label, strength). Strength 3: says exactly what it is; 1: could be something else.
LABELS = {
    "id": [("invoice number", 3), ("invoice no", 3), ("invoice #", 3), ("invoice num", 3), ("invoice id", 3),
           ("inv no", 3), ("inv #", 3), ("inv number", 3), ("invoice ref", 3), ("bill number", 2), ("bill no", 2),
           ("bill #", 2), ("document number", 2), ("document no", 2), ("receipt number", 2), ("receipt no", 2),
           ("credit note number", 3), ("credit note no", 3), ("credit note #", 3), ("credit memo number", 3),
           ("credit memo no", 3), ("credit memo #", 3), ("credit number", 3), ("credit no", 3), ("credit #", 2),
           ("reference", 1), ("ref no", 1), ("ref", 1), ("number", 1), ("no", 1), ("#", 1), ("invoice", 1),
           ("credit note", 1), ("credit memo", 1)],
    "date": [("invoice date", 3), ("date of invoice", 3), ("date of issue", 3), ("issue date", 3), ("issued on", 3),
             ("date issued", 3), ("credit note date", 3), ("credit date", 3), ("credit memo date", 3),
             ("billing date", 2), ("bill date", 2), ("document date", 2), ("tax point", 2),
             ("issued", 1), ("dated", 1), ("date", 2)],
    "amount": [("amount due", 3), ("balance due", 3), ("total due", 3), ("grand total", 3), ("invoice total", 3),
               ("total amount due", 3), ("total amount", 3), ("amount payable", 3), ("total payable", 3),
               ("please pay", 3), ("total to pay", 3), ("total incl", 3), ("total inc", 3), ("total usd", 3),
               ("total eur", 3), ("total gbp", 3), ("total credit", 3), ("credit total", 3), ("credit amount", 3),
               ("amount credited", 3), ("total refund", 3), ("refund amount", 3), ("total", 2), ("balance", 1),
               ("amount", 1)],
    "bank_account": [("iban", 3), ("bank account number", 3), ("bank account no", 3), ("bank account", 3),
                     ("account number", 2), ("account no", 2), ("acct no", 2), ("acct #", 2), ("account #", 2),
                     ("a c no", 2)],
    "subtotal": [("subtotal", 3), ("sub total", 3), ("invoice subtotal", 3), ("net total", 2), ("total net", 2), ("net amount", 2),
                 ("total before tax", 3), ("total excl", 3), ("amount before tax", 3)],
    "tax": [("sales tax", 3), ("vat", 3), ("gst", 3), ("hst", 3), ("pst", 3), ("tax amount", 3), ("total tax", 3),
            ("tax", 2)],
    "shipping": [("shipping and handling", 3), ("shipping & handling", 3), ("shipping", 3), ("freight", 2),
                 ("delivery charge", 3), ("handling", 2), ("other", 1)],
    "discount": [("total discount", 3), ("discount", 3), ("less discount", 3)],
    "paid": [("amount paid", 3), ("deposit received", 3), ("deposit", 2), ("paid", 2), ("less payment", 3)],
    "payable_to": [("make all checks payable to", 3), ("make checks payable to", 3), ("make all cheques payable to", 3),
                   ("checks payable to", 3), ("cheques payable to", 3), ("payable to", 3), ("remit to", 3),
                   ("pay to", 2), ("vendor name", 3), ("vendor", 3), ("supplier name", 3), ("supplier", 3),
                   ("seller", 3), ("from", 2), ("issued by", 3), ("company name", 1)],
    "customer": [("bill to", 3), ("billed to", 3), ("invoice to", 3), ("invoiced to", 3), ("sold to", 3),
                 ("ship to", 2), ("customer name", 3), ("customer", 2), ("client", 2), ("to", 1), ("buyer", 2)],
}
# Phrases that start like a label but mean something else.
NOT_LABEL = {
    "date": ("due date", "date due", "delivery date", "ship date", "shipping date", "order date", "date of delivery",
             "due", "payment date", "date paid", "period", "service date"),
    "amount": ("total discount", "total tax", "subtotal", "sub total", "total net", "total items", "total qty",
               "total quantity", "total hours", "total paid", "total before", "total excl", "amount paid",
               "amount in words", "total weight", "total units", "tax", "discount", "total due in",
               "total due within", "balance due in", "amount due in", "amount due within"),
    "id": ("invoice date", "invoice total", "invoice subtotal", "invoice to", "credit note date", "credit note total",
           "credit note to", "credit memo date", "credit memo total", "credit note for", "credit note against", "invoice amount", "invoice due", "number of", "no of",
           "no.of", "phone number", "tax number", "vat number", "account number", "order number", "po number",
           "customer number", "routing number", "reg number", "registration number", "number of items"),
    "tax": ("tax rate", "tax id", "tax number", "vat number", "vat no", "vat reg", "tax invoice", "taxable",
            "tax exempt", "tax %", "vat %", "gst no", "gst number", "gst %", "total tax rate"),
    "customer": ("total", "to pay", "to be paid"),
    "shipping": ("ship to", "shipping address", "shipping method", "ship date", "shipping date"),
    "payable_to": ("from date",),
}


def match_label(text: str, kind: str) -> tuple[int, str] | None:
    """(strength, the rest of the text after the label) if text starts with a label."""
    text = re.sub(r"(?i)^\s*(please|kindly)\s+", "", str(text))
    n = norm(text)
    if any(n.startswith(bad) for bad in NOT_LABEL.get(kind, ())):
        return None
    for label, strength in LABELS[kind]:
        if n == label or n.startswith(label + " ") or n.startswith(label + "#") or (label.endswith("#") and n.startswith(label)):
            rest = text.strip()
            # cut the label off the original text, keeping the value's own punctuation
            words = len(label.replace("#", " # ").split())
            parts = re.split(r"(\s+|:|#)", rest)
            seen, cut = 0, 0
            for i, part in enumerate(parts):
                if part.strip() and part not in (":", "#") and not re.fullmatch(r"\s+", part):
                    seen += 1
                elif part == "#" and "#" in label:
                    seen += 1
                if seen >= words:
                    cut = i + 1
                    break
            remainder = "".join(parts[cut:]).lstrip(" :#-–.\t").rstrip(" :\t") if seen >= words else ""
            if label in ("no", "#", "ref", "number", "to", "from", "other", "invoice", "date", "amount",
                         "balance", "customer", "client") and remainder and len(remainder) > 40:
                continue
            return strength, remainder
    return None


# ------------------------------------------------------------------ value parsers
def parse_value(kind: str, text: str, raw=None):
    """(value, quality 0..1) or (None, 0)."""
    t = str(text).strip().strip(":").strip()
    if not t:
        return None, 0
    if kind == "id":
        t = re.sub(r"(?i)^(no\.?|number|#|nr\.?)\s*", "", t).strip(" :#")
        if raw is not None and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            t = str(int(raw)) if float(raw).is_integer() else t
        if not re.search(r"\d", t) or not ID_PATTERN.match(t) or len(t) > 40:
            return None, 0
        if as_date(None, t)[0] or (money(t) is not None and "." in t and not re.search(r"[A-Za-z#-]", t)):
            return None, 0
        if re.fullmatch(r"\(?\+?\d[\d\s().-]{8,}", t) and " " in t:   # a phone number
            return None, 0
        return t, 1.0
    if kind == "date":
        iso, ambiguous = as_date(raw, t)
        return (iso, 0.7 if ambiguous else 1.0) if iso else (None, 0)
    if kind in ("amount", "subtotal", "tax", "shipping", "discount", "paid", "line"):
        value = money(raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else t)
        if value is None:
            return None, 0
        if kind == "amount" and value <= 0:
            return None, 0
        return value, 1.0
    if kind == "bank_account":
        value = account_value(t)
        return (value, 1.0 if iban_ok(value or "") else 0.8) if value else (None, 0)
    if kind in ("payable_to", "customer"):
        line = t.split("\n")[0].strip(" ,")
        if kind == "payable_to" and line.endswith(".") and not re.search(r"(?i)\b(co|inc|ltd|corp|bros|st)\.$", line):
            line = line[:-1]
        return (line, 1.0) if looks_like_name(line) else (None, 0)
    return None, 0


# ------------------------------------------------------------------ layout search
def _same_line(a: Box, b: Box) -> bool:
    if a.page != b.page:
        return False
    overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
    return overlap >= 0.5 * min(a.height, b.height)


def _is_any_label(box: Box) -> bool:
    return any(match_label(box.text, k) for k in ("id", "date", "amount", "subtotal", "tax", "customer",
                                                   "payable_to", "bank_account", "shipping", "discount"))


def find_value(boxes: list[Box], label: Box, kind: str, remainder: str):
    """The value belonging to a label: written after it, to its right, or below it."""
    if remainder:
        value, q = parse_value(kind, remainder)
        if value is not None:
            return value, q, label
    names = kind in ("payable_to", "customer")
    right = sorted((b for b in boxes if b is not label and _same_line(label, b) and b.x0 >= label.x1 - 2),
                   key=lambda b: b.x0)
    for b in right[:4]:
        if _is_any_label(b) and (names or parse_value(kind, b.text, b.raw)[0] is None):
            break
        value, q = parse_value(kind, b.text, b.raw)
        if value is not None:
            return value, q * 0.98, b
    if kind in ("subtotal", "tax", "shipping", "discount", "paid"):
        return None, 0, None   # these sit beside their label; "below" is usually a table column
    line = label.height
    below = sorted((b for b in boxes if b is not label and b.page == label.page and b.y0 >= label.y1 - line * 0.2
                    and b.y0 - label.y1 <= line * 3.2
                    and min(b.x1, label.x1) - max(b.x0, label.x0) > -line * 0.5
                    and abs(b.x0 - label.x0) < max(label.x1 - label.x0, line * 6)),
                   key=lambda b: (b.y0, abs(b.x0 - label.x0)))
    for b in below[:3]:
        if names and _is_any_label(b):
            break
        value, q = parse_value(kind, b.text, b.raw)
        if value is not None:
            return value, q * 0.9, b
        if _is_any_label(b):
            break
    return None, 0, None


STRENGTH_CONFIDENCE = {3: 0.95, 2: 0.8, 1: 0.6}


def best(boxes: list[Box], kind: str, prefer_low: bool = False):
    """The most convincing (value, confidence, source text, label box) for a field."""
    found = []
    for box in boxes:
        m = match_label(box.text, kind)
        if not m:
            continue
        strength, remainder = m
        value, q, src = find_value(boxes, box, kind, remainder)
        if value is None:
            continue
        position = (box.page, box.y0) if prefer_low else (-box.page, -box.y0)
        found.append((strength, position, value, STRENGTH_CONFIDENCE[strength] * q,
                      src.text if src else box.text, box, q))
    if not found:
        return None
    found.sort(key=lambda f: (f[0], f[1]), reverse=True)
    strength, _, value, conf, text, box, q = found[0]
    return value, round(conf, 2), text, box, q



def unlabelled_date(boxes: list[Box], id_box: Box | None):
    """A date with no label (some templates print just "Invoice #5465" with the date under
    it). Take the date nearest the invoice number, or else the highest one on the page,
    skipping any that belong to a "Due"/"Delivery" label."""
    found = []
    for b in boxes:
        iso, ambiguous = as_date(b.raw, b.text)
        if not iso or len(b.text) > 30:
            continue
        neighbours = [o for o in boxes if o is not b and o.page == b.page and (
            (_same_line(o, b) and o.x1 <= b.x0 + 2) or (0 <= b.y0 - o.y1 <= b.height * 1.5 and abs(o.x0 - b.x0) < 40))]
        if any(re.search(r"(?i)\b(due|delivery|ship|order|paid|period|expir)", o.text) for o in neighbours):
            continue
        if id_box is not None and id_box.page == b.page:
            distance = abs(b.y0 - id_box.y0) + abs(b.x0 - id_box.x0) * 0.5
        else:
            distance = b.page * 10_000 + b.y0
        found.append((distance, iso, ambiguous, b))
    if not found:
        return None
    _, iso, ambiguous, b = min(found, key=lambda f: f[0])
    return iso, 0.45 if ambiguous else 0.6, b.text


_BUSINESS = re.compile(r"(?i)\b(inc|llc|ltd|limited|corp|corporation|co|company|group|gmbh|plc|sa|bv|pty|"
                       r"services|solutions|studio|design|traders|trading|logistics|manufacturing|supplies|supply|"
                       r"consulting|partners|associates|systems|labs|foods|catering|freight|office|tooth|dental|"
                       r"clinic|shop|store|works|builders|electric|electrical|print|media|agency)\b|&|\band\b")


def looks_like_person(name: str) -> bool:
    words = name.split()
    return 2 <= len(words) <= 3 and not _BUSINESS.search(name) and all(w[:1].isupper() and w.isalpha() for w in words)


# ------------------------------------------------------------------ vendor
def _region_below(boxes: list[Box], label: Box, lines: int = 5) -> list[Box]:
    """The block of text under a label like "Bill to" (a customer's name and address)."""
    out = [label]
    for b in sorted(boxes, key=lambda b: b.y0):
        if b is label or b.page != label.page:
            continue
        if (_same_line(label, b) and b.x0 >= label.x1 - 2 and b.x0 - label.x1 < label.height * 12) or (
                label.y1 - label.height * 0.2 <= b.y0 <= label.y1 + label.height * 1.6 * lines
                and abs(b.x0 - label.x0) < label.height * 4):
            out.append(b)
    return out


def find_vendor(boxes: list[Box], company: str | None, ex: Extraction):
    # 1. Said outright: "Make all checks payable to X", "From: X", "Vendor: X".
    explicit = best(boxes, "payable_to")
    customer_boxes: set[int] = set()
    for box in boxes:
        m = match_label(box.text, "customer")
        if m and m[0] >= 2:
            region = _region_below(boxes, box)
            customer_boxes.update(id(b) for b in region)
            value, _, _ = find_value(boxes, box, "customer", m[1])
            if value and not ex.customer:
                ex.customer = value
    if explicit and company and similar(explicit[0], company) > 0.85:
        explicit = None
    if explicit and not looks_like_person(explicit[0]) and not any(
            similar(explicit[0], b.text) > 0.9 for b in boxes if b is not explicit[3] and b.page == 0):
        return explicit[0], explicit[1], explicit[2]

    # 2. The business name at the top: big, high up, not a label, not the customer, not an address.
    first_page = [b for b in boxes if b.page == 0] or boxes
    if not first_page:
        return None
    top = min(b.y0 for b in first_page)
    bottom = max(b.y1 for b in first_page)
    span = max(bottom - top, 1)
    sizes = sorted(b.size for b in first_page if b.size) or [0]
    body_size = sizes[len(sizes) // 2] or 1
    contact_text = " ".join(norm(b.text) for b in first_page if _CONTACT.search(b.text))
    candidates = []
    for b in first_page:
        if id(b) in customer_boxes or not looks_like_name(b.text) or _is_any_label(b):
            continue
        if company and similar(b.text, company) > 0.85:
            continue
        rel = (b.y0 - top) / span
        if rel > 0.45:
            continue
        score = (1 - rel) * 2
        if b.size:
            score += min(b.size / body_size, 2.5)
        tokens = [w for w in norm(b.text).split() if len(w) > 2 and w not in ("inc", "llc", "ltd", "the", "and", "corp")]
        if tokens and any(t in contact_text.replace(" ", "") for t in tokens):
            score += 1.5           # the name also appears in its email address or website
        candidates.append((score, b))
    if explicit:
        explicit_box = explicit[3]
        candidates = [c for c in candidates if c[1] is not explicit_box]
        same = [c for c in candidates if similar(c[1].text, explicit[0]) > 0.9]
        if same:   # the name at the top and the payee agree: as sure as it gets
            return same[0][1].text.strip(" ,"), 0.95, same[0][1].text
    if not candidates:
        return (explicit[0], explicit[1], explicit[2]) if explicit else None
    candidates.sort(key=lambda c: -c[0])
    score, b = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0
    confidence = 0.55 + min(0.3, (score - runner_up) / 6)
    if explicit:
        # Checks are payable to a person; the business name is at the top. The business is the
        # vendor, but paying an individual is worth a second look.
        ex.warnings.append(f"Payment is to be made to \"{explicit[0]}\", not to {b.text.strip()}. "
                           "Check that the vendor really asks for that.")
    return b.text.strip(" ,"), round(confidence, 2), b.text


# ------------------------------------------------------------------ line items
def line_items_total(boxes: list[Box], subtotal_box: Box | None, total_box: Box | None) -> float | None:
    """Sum of the line-item amounts: the numbers in the "Amount"/"Line total" column,
    between its header and the subtotal (or total) row."""
    headers = [b for b in boxes if norm(b.text) in ("amount", "line total", "total", "total price", "ext price",
                                                   "extended price", "net amount", "price total", "amount usd",
                                                   "amount $", "line amount", "value")]
    end_box = subtotal_box or total_box
    for header in sorted(headers, key=lambda b: (b.page, b.y0)):
        if end_box is not None and (header.page, header.y0) >= (end_box.page, end_box.y0):
            continue
        row = [b for b in boxes if _same_line(header, b) and b is not header]
        if not any(norm(b.text) in ("description", "item", "items", "qty", "quantity", "unit price", "price",
                                    "rate", "hours", "service", "product", "details") for b in row):
            continue
        total, count = 0.0, 0
        stops = [b for b in boxes if (b.page, b.y0) > (header.page, header.y1)
                 and any(match_label(b.text, k) for k in ("subtotal", "amount", "tax", "discount", "shipping", "paid"))
                 and not _same_line(header, b)]
        if stops:
            first = min(stops, key=lambda b: (b.page, b.y0))
            if end_box is None or (first.page, first.y0) < (end_box.page, end_box.y0):
                end_box = first
        for b in sorted(boxes, key=lambda b: (b.page, b.y0)):
            if (b.page, b.y0) < (header.page, header.y1 - 1):
                continue
            if end_box is not None and (b.page, b.y0) >= (end_box.page, end_box.y0 - 1):
                break
            overlap = min(b.x1, header.x1) - max(b.x0, header.x0)
            near_right = abs(b.x1 - header.x1) < header.height * 3
            if overlap > 0 or near_right:
                v = money(b.raw if isinstance(b.raw, (int, float)) else b.text)
                if v is not None:
                    total += v
                    count += 1
        if count:
            return round(total, 2)
    return None


# ------------------------------------------------------------------ main
def find_fields(boxes: list[Box], company: str | None = None, method: str = "") -> Extraction:
    ex = Extraction(method=method, pages=1 + max((b.page for b in boxes), default=0))
    boxes = [b for b in boxes if b.text and b.text.strip()]
    if not boxes:
        ex.warnings.append("No text was found in this file.")
        return ex
    if is_credit_note(boxes):
        ex.kind = "credit"
    token = _CREDIT_NOTE.set(ex.kind == "credit")
    try:
        return _find_fields(ex, boxes, company)
    finally:
        _CREDIT_NOTE.reset(token)


_CREDIT_TITLE = re.compile(r"(?i)^\s*(tax |vat )?(credit (note|memo|memorandum|invoice)|creditnote|gutschrift|avoir|"
                           r"nota de cr[eé]dito|creditnota)\b")


def is_credit_note(boxes: list[Box]) -> bool:
    """A credit note says so in its title, or labels its number "Credit note no."."""
    first = [b for b in boxes if b.page == 0]
    if not first:
        return False
    top = min(b.y0 for b in first)
    span = max(max(b.y1 for b in first) - top, 1)
    return any(_CREDIT_TITLE.match(b.text) and (b.y0 - top) / span < 0.5 for b in first)


def _find_fields(ex: Extraction, boxes: list[Box], company: str | None) -> Extraction:

    id_box = None
    for kind in ("id", "date", "bank_account"):
        hit = best(boxes, kind)
        if hit:
            ex.fields[kind], ex.confidence[kind], ex.source[kind] = hit[0], hit[1], hit[2]
            if kind == "id":
                id_box = hit[3]
            if kind == "date" and hit[4] < 0.8:
                ex.warnings.append("The date could be read two ways (month/day or day/month). Check it.")
    if "date" not in ex.fields:
        hit = unlabelled_date(boxes, id_box)
        if hit:
            ex.fields["date"], ex.confidence["date"], ex.source["date"] = hit
    if "bank_account" not in ex.fields:
        for b in boxes:   # an IBAN is recognisable on its own, label or not
            m = re.search(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{2,4}){3,8})\b", b.text.upper())
            if m and iban_ok(m.group(1)):
                ex.fields["bank_account"], ex.confidence["bank_account"] = re.sub(r"\s", "", m.group(1)), 0.85
                ex.source["bank_account"] = b.text
                break

    total = best(boxes, "amount", prefer_low=True)
    subtotal = best(boxes, "subtotal", prefer_low=True)
    if total:
        ex.fields["amount"], ex.confidence["amount"], ex.source["amount"] = total[0], total[1], total[2]
    for kind in ("subtotal", "tax", "shipping", "discount", "paid"):
        hit = subtotal if kind == "subtotal" else best(boxes, kind, prefer_low=True)
        if hit:
            ex.totals[kind] = hit[0]
    lines = line_items_total(boxes, subtotal[3] if subtotal else None, total[3] if total else None)
    if lines is not None:
        ex.totals["lines"] = lines
    if "amount" not in ex.fields and "subtotal" in ex.totals:
        ex.fields["amount"] = round(ex.totals["subtotal"] + ex.totals.get("tax", 0) + ex.totals.get("shipping", 0)
                                    - ex.totals.get("discount", 0), 2)
        ex.confidence["amount"], ex.source["amount"] = 0.5, "subtotal + tax"
        ex.warnings.append("No total was printed; the amount was worked out from the subtotal and tax.")

    vendor = find_vendor(boxes, company, ex)
    if vendor:
        ex.fields["vendor"], ex.confidence["vendor"], ex.source["vendor"] = vendor
    if ex.customer and company and similar(ex.customer, company) < 0.6 and len(norm(ex.customer).split()) > 1:
        ex.warnings.append(f"The invoice is addressed to \"{ex.customer}\", not {company}. Check it is yours.")

    ex.currency = next((c for c in (currency_of(b.text) for b in boxes) if c), None)
    issue = totals_issue(ex.fields.get("amount"), ex.totals)
    if issue:
        ex.warnings.append(issue)
    elif "subtotal" in ex.totals and ex.confidence.get("amount", 0) >= 0.6:
        ex.confidence["amount"] = 0.97   # the printed arithmetic agrees with the total we read
    for f in REQUIRED:
        if not ex.fields.get(f):
            ex.warnings.append(f"Couldn't find the {LABEL_NAMES[f].lower()}. Type it in.")
    if ex.kind == "credit":
        ex.warnings.insert(0, "This is a credit note: it reduces what you owe this vendor.")
    return ex


LABEL_NAMES = {"id": "Invoice number", "vendor": "Vendor", "date": "Invoice date", "amount": "Total amount",
               "bank_account": "Bank account"}


def totals_issue(amount: float | None, totals: dict) -> str | None:
    """A sentence if the invoice's own arithmetic doesn't add up, else None.

    Checks two things a printed invoice promises: the line items add up to the subtotal,
    and subtotal + tax + shipping (- discount) equals the total. Several readings are
    allowed (discount already taken off, or not; deposit already paid, or not), so only a
    real inconsistency is reported.
    """
    sub, lines = totals.get("subtotal"), totals.get("lines")
    if sub is not None and lines is not None and abs(lines - sub) > 0.015 and lines > 0:
        disc = totals.get("discount", 0)
        if abs(lines - disc - sub) > 0.015:
            return f"The line items add up to {lines:,.2f}, but the subtotal says {sub:,.2f}."
    if amount is None or sub is None:
        return None
    extras = totals.get("tax", 0) + totals.get("shipping", 0)
    disc, paid = totals.get("discount", 0), totals.get("paid", 0)
    readings = {sub + extras, sub + extras - disc, sub + extras - paid, sub + extras - disc - paid,
                sub, sub - disc}
    if any(abs(r - amount) <= 0.015 for r in readings):
        return None
    detail = f"subtotal {sub:,.2f}"
    if totals.get("tax"):
        detail += f" + tax {totals['tax']:,.2f}"
    if totals.get("shipping"):
        detail += f" + shipping {totals['shipping']:,.2f}"
    if disc:
        detail += f" - discount {disc:,.2f}"
    return f"The totals don't add up: {detail} = {sub + extras - disc:,.2f}, but the total says {amount:,.2f}."


# ------------------------------------------------------------------ readers
def kind_of(filename: str, data: bytes) -> str:
    """What the file really is, from its first bytes (the name can lie)."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "xlsx"
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff") or data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    return "unknown"


def boxes_from_xlsx(data: bytes) -> list[Box]:
    from openpyxl import load_workbook
    _check_xlsx(data)
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True)
    except Exception as exc:
        raise FileProblem("This Excel file could not be opened. Re-save it as .xlsx.") from exc
    ws = next((w for w in [wb.active, *wb.worksheets] if w is not None and w.max_row and w.max_column), wb.active)
    widths = {}
    for letter, dim in ws.column_dimensions.items():
        if dim.width:
            widths[letter] = dim.width
    from openpyxl.utils import get_column_letter
    col_x = [0.0]
    for c in range(1, min(ws.max_column, 60) + 2):
        col_x.append(col_x[-1] + widths.get(get_column_letter(c), 9.0) * 7)
    merged = {}
    for rng in ws.merged_cells.ranges:
        merged[(rng.min_row, rng.min_col)] = rng.max_col
    boxes, seen = [], 0
    for row in ws.iter_rows(max_row=min(ws.max_row, 400), max_col=min(ws.max_column, 60)):
        for cell in row:
            if cell.value is None or (isinstance(cell.value, str) and not cell.value.strip()):
                continue
            seen += 1
            if seen > MAX_CELLS:
                break
            value = cell.value
            if isinstance(value, float) and value.is_integer():
                text = str(int(value))
            elif isinstance(value, (datetime, date)):
                text = value.strftime("%Y-%m-%d")
            elif isinstance(value, float):
                text = f"{value:.2f}"
            else:
                text = str(value)
            last_col = merged.get((cell.row, cell.column), cell.column)
            size = float(cell.font.sz or 11) if cell.font else 11.0
            if cell.font and cell.font.b:
                size *= 1.15
            y = (cell.row - 1) * 20.0
            boxes.append(Box(text.strip(), col_x[cell.column - 1], y, col_x[last_col], y + 15.0, 0, size, value))
    return boxes


def _merge_words(words: list[dict], page: int) -> list[Box]:
    """Join words on the same line into phrases when the gap between them is a normal
    space; a wider gap starts a new box (a new column on the form)."""
    lines: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"] / 3), w["x0"])):
        for line in lines:
            ref = line[-1]
            if abs(ref["top"] - w["top"]) < max(ref["bottom"] - ref["top"], 4) * 0.5:
                line.append(w)
                break
        else:
            lines.append([w])
    boxes = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        current = [line[0]]
        for w in line[1:]:
            size = max(max(float(x.get("size") or 10) for x in current), float(w.get("size") or 10), 4)
            if w["x0"] - current[-1]["x1"] <= size * 0.6:
                current.append(w)
            else:
                boxes.append(_box(current, page))
                current = [w]
        boxes.append(_box(current, page))
    return boxes


def _box(words: list[dict], page: int) -> Box:
    return Box(" ".join(w["text"] for w in words), min(w["x0"] for w in words), min(w["top"] for w in words),
               max(w["x1"] for w in words), max(w["bottom"] for w in words), page,
               max(float(w.get("size") or 0) for w in words))


def boxes_from_pdf(data: bytes, ocr: bool = True) -> tuple[list[Box], str]:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise FileProblem("PDF support isn't installed on this server (pdfplumber).") from exc
    boxes, scanned = [], []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise FileProblem(f"This PDF has {len(pdf.pages)} pages. Upload one invoice per file "
                                  f"(at most {MAX_PAGES} pages).")
            for i, page in enumerate(pdf.pages):
                words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False, use_text_flow=False)
                if sum(len(w["text"]) for w in words) < MIN_PDF_TEXT:
                    scanned.append(i)
                    if ocr and ocr_available():
                        image = page.to_image(resolution=300).original
                        scale = page.width / image.width
                        boxes += [Box(b.text, b.x0 * scale, b.y0 * scale, b.x1 * scale, b.y1 * scale, i, b.size * scale)
                                  for b in boxes_from_image_obj(image, i)]
                    continue
                boxes += _merge_words(words, i)
    except FileProblem:
        raise
    except Exception as exc:
        raise FileProblem("This PDF could not be read. It may be damaged or password-protected.") from exc
    if scanned and not boxes:
        if not ocr_available():
            raise FileProblem("This PDF is a scan (a picture of the page, with no text in it). Reading scans "
                              "needs OCR (Tesseract) on the server, or AI reading switched on in Settings.")
        return boxes, "OCR (scanned PDF)"
    return boxes, "OCR (scanned PDF)" if scanned else "PDF text"


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def boxes_from_image_obj(image, page: int = 0) -> list[Box]:
    import pytesseract
    from PIL import ImageOps
    gray = ImageOps.grayscale(image)
    if gray.width < 1600:   # small photos read better enlarged
        factor = 1600 / gray.width
        gray = gray.resize((1600, int(gray.height * factor)))
    else:
        factor = 1.0
    # "Sparse text" mode finds scattered labels and values, which suits forms better than
    # the default page-of-paragraphs mode.
    data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT, config="--psm 11")
    words = []
    for i, text in enumerate(data["text"]):
        if not text.strip() or float(data["conf"][i]) < 30:
            continue
        x, y, w, h = (data[k][i] / factor for k in ("left", "top", "width", "height"))
        words.append({"text": text, "x0": x, "x1": x + w, "top": y, "bottom": y + h, "size": h})
    return _merge_words(words, page)


def boxes_from_image(data: bytes) -> list[Box]:
    if not ocr_available():
        raise FileProblem("Reading photos and scans needs OCR (Tesseract) on the server, or AI reading "
                          "switched on in Settings.")
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS   # refuse "decompression bomb" images
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise FileProblem("This image could not be opened, or is too large.") from exc
    return boxes_from_image_obj(image.convert("RGB"))


def read_document(filename: str, data: bytes, company: str | None = None) -> Extraction:
    """The whole local pipeline for one file. Raises FileProblem for unusable files."""
    if not data:
        raise FileProblem("The file is empty.")
    if len(data) > MAX_BYTES:
        raise FileProblem(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    kind = kind_of(filename, data)
    if kind == "xlsx":
        boxes, method = boxes_from_xlsx(data), "Excel layout"
    elif kind == "pdf":
        boxes, method = boxes_from_pdf(data)
    elif kind == "image":
        boxes, method = boxes_from_image(data), "OCR (image)"
    else:
        raise FileProblem("Upload a PDF, an Excel invoice (.xlsx), or a photo or scan (.png, .jpg).")
    ex = find_fields(boxes, company, method)
    if method.startswith("OCR"):
        for f in ex.confidence:   # OCR can misread characters: always worth a look
            ex.confidence[f] = round(min(ex.confidence[f], 0.8), 2)
    return ex


def document_text(filename: str, data: bytes) -> str:
    """Plain text of an Excel invoice laid out row by row (for the optional AI step)."""
    boxes = boxes_from_xlsx(data)
    rows: dict[float, list[Box]] = {}
    for b in boxes:
        rows.setdefault(b.y0, []).append(b)
    return "\n".join(" | ".join(b.text for b in sorted(r, key=lambda b: b.x0)) for _, r in sorted(rows.items()))
