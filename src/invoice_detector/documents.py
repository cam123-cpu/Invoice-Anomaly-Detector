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

from . import doc_languages as lang
from . import einvoice
from .importer import ID_PATTERN, FileProblem, MAX_BYTES, _check_xlsx, parse_date

MAX_BOXES = 4000         # text boxes in one file; the largest real invoice tested has 269
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
    candidates: dict = field(default_factory=dict)   # field -> [[label, value], ...] seen on the page
    learned: dict = field(default_factory=dict)      # field -> the learned label it was read beside

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
    text = unicodedata.normalize("NFKD", str(text).replace("ß", "ss")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9#%&@.$€£ ]+", " ", text.lower().replace("nº", "no").replace("n°", "no"))
    return re.sub(r"\s+", " ", text).strip(" .")


_MONEY = re.compile(r"^[^\d]{0,4}\(?-?\s*\d[\d,.'  ]*\)?\s*(?:[A-Za-z$€£¥]{1,3})?$")
_CURRENCY_SIGNS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_CURRENCY_CODES = ("USD", "EUR", "GBP", "CAD", "AUD", "CHF", "JPY", "INR", "NZD", "SEK", "NOK", "DKK")


# On a credit note the amounts are often printed negative (-120.00 or (120.00)). While one is
# being read this is switched on, and such amounts are taken at their size.
_CREDIT_NOTE = contextvars.ContextVar("credit_note", default=False)


# Longest text a single value is read from. Real values are far shorter; the cap keeps every
# parser linear in the size of the document, however a hostile PDF lays out its text.
MAX_VALUE_TEXT = 200


def money(value) -> float | None:
    """A non-negative amount, or None. Accepts zero (unlike the importer), since a tax or
    discount line is often 0.00."""
    if isinstance(value, bool) or (isinstance(value, str) and len(value) > MAX_VALUE_TEXT):
        return None
    if isinstance(value, (int, float)):
        if value < 0 and _CREDIT_NOTE.get():
            value = -value
        return round(float(value), 2) if value >= 0 else None
    text = str(value or "").strip()
    text = re.sub(r"(?i)\s*\b(t\.?t\.?c|h\.?t|brutto|netto)\b\.?$", "", text)          # "29,99 € TTC"
    text = re.sub(r"(?<=\d) (?=\d{3}(?: \d{3})*[.,]\d{2}(?:\D|$))", "", text)           # "120 000,00"
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


# Is the day written first in this document's dates? None: unknown (03/04 is then read
# month-first and marked ambiguous). "likely": probably (a pound or euro sign). "sure":
# another date on the page settles it, or the document isn't in English.
_DAY_FIRST = contextvars.ContextVar("day_first", default=None)

_DATE_IN_TEXT = re.compile(
    r"\b\d{1,4}[./-]\d{1,2}[./-]\d{2,4}\b"
    r"|\b\d{1,2}(?:er|\.|st|nd|rd|th)?\s+(?:de\s+)?[A-Za-zÀ-ÿ]{3,10}\.?,?\s+(?:de\s+)?\d{4}\b"
    r"|\b[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b")


_OTHER_DATE = re.compile(r"\b(" + "|".join(lang.OTHER_DATE_WORDS) + ")")


def _english_months(text: str) -> str:
    """'7. Mai 2014', '02 juillet 2015', '5 de marzo de 2024' -> '7 May 2014' and so on."""
    text = re.sub(r"^(\d{1,2})(?:\.|er)\s*(?=[A-Za-zÀ-ÿ])", r"\1 ", text)
    text = re.sub(r"(?i)\s+(de|del|di)\s+", " ", text)

    def swap(m):
        word = unicodedata.normalize("NFKD", m.group(0)).encode("ascii", "ignore").decode().lower()
        number = lang.MONTHS.get(word)
        return lang.ENGLISH_MONTHS[number - 1] if number else m.group(0)
    return re.sub(r"[A-Za-zÀ-ÿ]{3,10}", swap, text).replace(".", " ") if re.search(r"[A-Za-zÀ-ÿ]{3}", text) else text


def as_date(value, text: str) -> tuple[str | None, bool]:
    """(ISO date or None, ambiguous?). 03/04/2026 could be March or April. If the document
    shows that it writes the day first (see _DAY_FIRST) it is read that way; otherwise
    month-first (US style) and reported as ambiguous so a person checks it. A date written
    with dots (03.04.2026) is always day-first."""
    if isinstance(value, datetime):
        return value.date().isoformat(), False
    if len(str(text or "")) > MAX_VALUE_TEXT:
        return None, False
    if isinstance(value, date):
        return value.isoformat(), False
    text = re.sub(r"(?i)\b(\d{1,2})(st|nd|rd|th)\b", r"\1", str(text or "")).strip(" ,.")
    text = re.sub(r"(?i)^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+", "", text)
    text = re.sub(r"(?i)^(le|den|am|vom|du|el|il)\s+(?=\d)", "", text)
    text = re.sub(r"\s+,", ",", text)                                   # "August 3 , 2014"
    text = re.sub(r"\s+", " ", _english_months(text)).strip()
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
    if dmy and re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{2,4}", text):
        return dmy, False
    if mdy and dmy and mdy != dmy:
        hint = _DAY_FIRST.get()
        if hint:
            return dmy, hint != "sure"
        return mdy, True
    return mdy or dmy, False


def date_order_hint(boxes: list) -> str | None:
    """Work out from the whole document whether its dates are day-first."""
    text = " ".join(str(b.text) for b in boxes)
    day_first = month_first = 0
    for a, b, _ in re.findall(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text):
        a, b = int(a), int(b)
        if a > 12 >= b >= 1:
            day_first += 1
        elif b > 12 >= a >= 1:
            month_first += 1
    if month_first:
        return None
    if day_first:
        return "sure"
    words = set(norm(text).replace(".", " ").split())
    if len(words & set(lang.FOREIGN_MARKERS)) >= 2 or re.search(r"\b\d{1,2}\.\d{1,2}\.(19|20)\d{2}\b", text):
        return "sure"
    if re.search(r"[£€]|\b(GBP|EUR|AUD|NZD|INR|CHF)\b", text):
        return "likely"
    return None


_ADDRESS = re.compile(
    r"(?i)(^\d+\s+\w+)|\b(street|st\.?|avenue|ave\.?|road|rd\.?|suite|ste\.?|blvd|boulevard|lane|ln\.?|"
    r"drive|dr\.?|floor|p\.?o\.? box|city|state|zip|postcode|address)\b|\b\d{5}(-\d{4})?\b|"
    r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b|,\s*[A-Z]{2}\s+\d{5}|"
    # continental Europe: "Hauptstraße 20", "Uferweg 40-42", "rue de la Paix", "3012 CN Rotterdam", "D-63571 Ort"
    r"\b\w*(stra[sß]e|str\.|weg|platz|allee|gasse|laan|straat|plein|gracht|kade)\s+\d|"
    r"\b(rue|avenue|chemin|boulevard|place|all[ée]e|impasse|quai|calle|avenida|plaza|via|viale|piazza|corso)\s+\w|"
    r"^\d{4}\s?[A-Z]{2}\s+\w|^[A-Z]{1,2}-\d{4,5}\s|^\d{4}\s+[A-ZÀ-Ý]\w+$|"
    r"^(deutschland|germany|france|nederland|netherlands|the netherlands|belgi[eë]|belgique|[öo]sterreich|austria|"
    r"schweiz|suisse|switzerland|espa[ñn]a|spain|italia|italy|united kingdom|uk|usa|united states|hong kong|"
    r"india|canada|australia|ireland|luxembourg|portugal)$")
_HOUSE_NUMBER = re.compile(r"^[A-Za-zÀ-ÿ.' -]{3,40}\s\d{1,4}\s?[A-Za-z]?(-\d{1,4})?$")   # "Weena 664"
_PERSON_PREFIX = re.compile(r"(?i)^(herrn?|frau|monsieur|madame|mme|mr|mrs|ms|dhr|mevr|sr|sra|sig|t\.a\.v|z\.\s?hd|"
                            r"c/o|attn|att|attention|a l'attention)\b\.?")
_DOC_WORD = re.compile(r"(?i)\b(" + "|".join(re.escape(w) for w in lang.DOCUMENT_WORDS) + r")")
_LEGAL = {re.sub(r"[^a-z0-9]", "", f) for f in lang.LEGAL_FORMS}


def has_legal_form(name: str) -> bool:
    """Does the name carry a company's legal form (GmbH, B.V., SARL, Ltd...)?"""
    words = [re.sub(r"[^a-z0-9]", "", w) for w in norm(name).split()]
    return any(w in _LEGAL for w in words[1:])
_CONTACT = re.compile(r"(?i)@|www\.|https?://|\.com\b|\.co\b|\.org\b|\.net\b|^\(?\+?\d[\d\s().-]{6,}$|"
                      r"\b(tel|phone|fax|email|e-mail|mobile)\b")
_NOT_A_NAME = {"eur", "usd", "gbp", "chf", "euro", "seite", "page", "pagina", "blatt", "datum", "betrag", "menge", "preis",
               "bezeichnung", "beschreibung", "designation", "quantite", "prix", "omschrijving", "aantal", "prijs",
               "descripcion", "cantidad", "precio", "descrizione", "quantita", "prezzo", "pos", "artikel","credit note", "credit memo", "tax credit note", "credit invoice", "credit", "invoice", "tax invoice", "commercial invoice", "sales invoice", "proforma invoice",
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
    if _DOC_WORD.search(norm(t)) or _PERSON_PREFIX.match(t) or re.search(r":\s|:$|^\(c\)|©|^\(", t):
        return False
    if _HOUSE_NUMBER.match(t) and not has_legal_form(t):
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
    "amount": [("amount due", 3), ("balance due", 3), ("total due", 3), ("due payable", 3), ("total payable amount", 3), ("grand total", 3), ("invoice total", 3),
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


def _plain(n: str) -> str:
    """Normalised text without the dots of abbreviations: 'rechnungsnr. 12' -> 'rechnungsnr 12'."""
    return re.sub(r"(?<=[a-z0-9])\.(?= |$)", "", n)


def _table(kind: str) -> list[tuple[str, int, bool]]:
    """Every label for a field, in every language: (label, strength, needs a colon).
    Longest first, so the most specific label wins."""
    rows = [(label, strength, False) for label, strength in LABELS[kind]]
    rows += [(_plain(norm(label.rstrip(":"))), strength, label.endswith(":"))
             for label, strength in lang.LABELS.get(kind, ())]
    return sorted(dict.fromkeys(rows), key=lambda r: -len(r[0]))


_TABLES = {kind: _table(kind) for kind in LABELS}
_NOT_FOREIGN = {kind: tuple(_plain(norm(p)) for p in phrases) for kind, phrases in lang.NOT_LABEL.items()}
_GENERIC = {"no", "#", "ref", "number", "to", "from", "other", "invoice", "date", "amount", "balance", "customer",
            "client"} | {label for rows in _TABLES.values() for label, strength, _ in rows if strength == 1}

# A title that carries the number and date itself: "Handelsrechnung Nr. 471102 vom 05.06.2018",
# "Facture n°562044387 du 02 Juillet 2015", "Tax Invoice No: 77 dated 3 May 2024".
_DOC = (r"(?:rechnung|rechnungskorrektur|gutschrift|beleg|invoice|bill|credit note|credit memo|facture|avoir|"
        r"factuur|creditnota|factura|fattura)")
_INLINE_ID = re.compile(r"(?i)\b[a-zäöü]*" + _DOC + r"s?(?:\s*\([^)]{0,50}\))*[\s.-]*"
                        r"(?:nr|no|n°|nº|num|nummer|number|numéro|numero|#)"
                        r"(?![a-zé])[.:]?\s*(\S.*)$")
_INLINE_DATE = re.compile(r"(?i)\b[a-zäöü]*" + _DOC + r"\b.{0,80}?\s(?:vom|du|dated|del|van|d\.d\.)(?:\s+(\S.*))?$")
_ORDER_WORDS = re.compile(r"(?i)bestell|commande|\border\b|lieferschein|pedido|ordine|\bpo\b|purchase")


def match_label(text: str, kind: str) -> tuple[int, str] | None:
    """(strength, the rest of the text after the label) if text starts with a label."""
    text = re.sub(r"(?i)^\s*(please|kindly)\s+", "", str(text))
    text = re.sub(r"^([A-Z]) (?=[a-z]{4})", r"\1", text)      # a kerned capital: "F actuurnummer"
    text = re.sub(r"(?i)\bn[°º]\s*", "n° ", text)             # "n°5620" -> "n° 5620"
    n = _plain(norm(text))
    if any(n.startswith(bad) for bad in NOT_LABEL.get(kind, ())):
        return None
    if any(n == bad or n.startswith(bad + " ") for bad in _NOT_FOREIGN.get(kind, ())):
        return None
    for label, strength, needs_colon in _TABLES[kind]:
        if n == label or n.startswith(label + " ") or n.startswith(label + "#") or (label.endswith("#") and n.startswith(label)):
            if needs_colon and (":" not in text or _plain(norm(text.split(":")[0])) != label):
                continue
            rest = text.strip()
            # cut the label off the original text, keeping the value's own punctuation
            words = len(label.replace("#", " # ").split())
            parts = re.split(r"(\s+|:|#|/|'|’|(?<=[A-Za-zÀ-ÿ])-(?=[A-Za-zÀ-ÿ])|(?<=[A-Za-z])\.(?=[A-Za-z-]))", rest)
            seen, cut = 0, 0
            for i, part in enumerate(parts):
                if not part:
                    continue
                if part == "#" and "#" in label:
                    seen += 1
                elif part.strip() and part not in (":", "#", "/", "'", "’", "-", ".") and norm(part):
                    seen += 1
                if seen >= words:
                    cut = i + 1
                    break
            remainder = "".join(p for p in parts[cut:] if p).lstrip(" :#-–.\t°º").rstrip(" :\t") if seen >= words else ""
            if label in _GENERIC and remainder and len(remainder) > 40:
                continue
            return strength, remainder
    if kind == "id":
        m = _INLINE_ID.search(text)
        if m:
            return 3, m.group(1).strip()
    if kind == "date" and not _ORDER_WORDS.search(text):
        m = _INLINE_DATE.search(text)
        if m:
            return 3, (m.group(1) or "").strip()
    return None


# ------------------------------------------------------------------ value parsers
def parse_value(kind: str, text: str, raw=None):
    """(value, quality 0..1) or (None, 0)."""
    t = str(text).strip().strip(":").strip()
    if not t or len(t) > MAX_VALUE_TEXT:
        return None, 0          # no field is that long; and the parsers below must stay cheap
    if kind == "id":
        t = re.sub(r"(?i)^(no\.?|number|#|nr\.?)\s*", "", t).strip(" :#")
        if raw is not None and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            t = str(int(raw)) if float(raw).is_integer() else t
        # The number ends where ordinary words begin: "471102 vom 05.06.2018", "30064443 Kundennr. 47774".
        kept = []
        for token in t.split():
            bare = token.strip(".,;:()")
            if not re.search(r"\d", bare) and re.search(r"[a-zà-ÿ]", bare):
                break
            if kept and (as_date(None, bare)[0] or re.fullmatch(r"\d{1,2}", bare) and _DATE_IN_TEXT.search(t)):
                break
            kept.append(token)
        t = " ".join(kept).strip(" .,;:")
        if not re.search(r"\d", t) or not ID_PATTERN.match(t) or len(t) > 40:
            return None, 0
        if as_date(None, t)[0] or (money(t) is not None and "." in t and not re.search(r"[A-Za-z#-]", t)):
            return None, 0
        if re.fullmatch(r"\(?\+?\d[\d\s().-]{8,}", t) and " " in t:   # a phone number
            return None, 0
        return t, 1.0
    if kind == "date":
        iso, ambiguous = as_date(raw, t)
        if not iso and len(t) <= 70:       # a date inside a phrase: "13/11/2017 Contact commercial"
            m = _DATE_IN_TEXT.search(t)
            before = norm(t[:m.start()]) if m else ""
            if m and not _OTHER_DATE.search(before):
                iso, ambiguous = as_date(None, m.group(0))
        return (iso, 0.7 if ambiguous else 1.0) if iso else (None, 0)
    if kind in ("amount", "subtotal", "tax", "shipping", "discount", "paid", "line"):
        value = money(raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else t)
        quality = 1.0
        if value is None and isinstance(text, str) and len(t) <= 70:
            # Several things in one phrase: "20,0% 81,90 € 16,38 €", "EUR 24.99 5.00 29.99".
            joined = re.sub(r"(?<=\d) (?=\d{3}(?: \d{3})*[.,]\d{2}(?:\D|$))", "", t)
            found = [money(tok) for tok in re.findall(r"[-(]?\d[\d.,']*[.,]\d{2}\)?(?![\d%])", joined)]
            found = [v for v in found if v is not None]
            if found:
                value, quality = (found[0] if kind in ("subtotal", "line") else found[-1]), 0.9
        if value is None:
            return None, 0
        if kind == "amount" and value <= 0:
            return None, 0
        return value, quality
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
    if kind == "amount":
        # A totals row with columns: net, tax, gross. When the first two add up to the third,
        # the third is the total, whatever order the labels came in.
        row = [(parse_value("amount", b.text, b.raw)[0], b) for b in right[:5]]
        row = [(v, b) for v, b in row if v is not None]
        if len(row) >= 3 and abs(row[-3][0] + row[-2][0] - row[-1][0]) <= 0.015 and row[-2][0] < row[-3][0]:
            return row[-1][0], 0.98, row[-1][1]
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
_COLUMN_HEADINGS = {"description", "item", "items", "qty", "quantity", "unit price", "price", "rate", "hours", "service",
                    "product", "details", "vat", "tax", "unit", "units", "sku", "code", "article", "pos", "no", "#",
                    "bezeichnung", "beschreibung", "menge", "einzelpreis", "preis", "artikel", "mwst", "ust", "einheit",
                    "designation", "quantite", "prix unitaire", "prix", "tva", "p.u", "p.u. ht", "nombre",
                    "omschrijving", "aantal", "prijs", "prijs per stuk", "btw", "stuks",
                    "descripcion", "cantidad", "precio", "iva", "descrizione", "quantita", "prezzo"}


def _in_table_header(box: Box, boxes: list[Box]) -> bool:
    """Is this box one of the column headings of the line-item table?"""
    row = [norm(b.text) for b in boxes if b is not box and _same_line(box, b)]
    return sum(1 for text in row if text in _COLUMN_HEADINGS) >= 2


# Labels reviewers taught the reader for the vendor being read: {field: [label keys]}.
_LEARNED = contextvars.ContextVar("learned", default=None)


def label_key(text: str, remainder: str = "") -> str:
    """A label as it can be recognised again on the next invoice: lower case, no digits or
    punctuation, without the value that followed it ("Rechnungsnr. 30064443" -> "rechnungsnr")."""
    t = norm(text)
    if remainder:
        t = t.replace(norm(remainder), " ")
    t = re.sub(r"[^a-z ]+", " ", t)
    return " ".join(t.split())[:60]


def all_candidates(boxes: list[Box], kind: str) -> list[tuple]:
    """Every labelled value on the page for a field, best first:
    (strength, position, value, confidence, source text, label box, quality, value box, label key)."""
    found = []
    for box in boxes:
        m = match_label(box.text, kind)
        if not m:
            continue
        strength, remainder = m
        if kind == "amount" and not remainder and _in_table_header(box, boxes):
            continue               # "Total" heading a column of line totals, not the invoice total
        value, q, src = find_value(boxes, box, kind, remainder)
        if value is None:
            continue
        position = (box.page, box.y0)
        found.append((strength, position, value, STRENGTH_CONFIDENCE[strength] * q,
                      src.text if src else box.text, box, q, src or box, label_key(box.text, remainder)))
    return found


def best(boxes: list[Box], kind: str, prefer_low: bool = False):
    """The most convincing (value, confidence, source text, label box) for a field."""
    found = all_candidates(boxes, kind)
    if not found:
        return None
    if not prefer_low:
        found = [(f[0], (-f[1][0], -f[1][1]), *f[2:]) for f in found]
    learned = (_LEARNED.get() or {}).get(kind)
    if learned:
        # A reviewer showed which label this vendor prints the value beside: take that one.
        taught = [f for f in found if f[8] in learned]
        if taught:
            taught.sort(key=lambda f: (learned.index(f[8]), f[1]))
            strength, _, value, conf, text, box, q, src, key = taught[0]
            return value, max(round(conf, 2), 0.9), text, box, q, src
    if prefer_low:
        # Totals: a figure printed under several labels ("Total", "Amount due") beats one printed once.
        top = max(f[0] for f in found)
        times: dict = {}
        for f in found:
            if f[0] == top:
                times[f[2]] = times.get(f[2], 0) + 1
        found.sort(key=lambda f: (f[0], times.get(f[2], 0) if f[0] == top else 0, f[1]), reverse=True)
    else:
        found.sort(key=lambda f: (f[0], f[1]), reverse=True)
    strength, _, value, conf, text, box, q, src, key = found[0]
    return value, round(conf, 2), text, box, q, src



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
        if any(_OTHER_DATE.search(norm(o.text)) for o in neighbours):
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
            similar(explicit[0], b.text) > 0.9 for b in boxes
            if b is not explicit[3] and b is not explicit[5] and b.page == 0):
        return explicit[0], explicit[1], explicit[2]

    # 1b. A "Seller" heading with the details listed under it (often as "Name: ...").
    listed = _seller_section(boxes)
    if listed and not (company and similar(listed.text, company) > 0.85):
        return listed.text.strip(" ,"), 0.9, listed.text

    # 2. The business name at the top: big, high up, not a label, not the customer, not an address.
    first_page = [b for b in boxes if b.page == 0] or boxes
    if not first_page:
        return None
    top = min(b.y0 for b in first_page)
    bottom = max(b.y1 for b in first_page)
    span = max(bottom - top, 1)
    sizes = sorted(b.size for b in first_page if b.size) or [0]
    body_size = sizes[len(sizes) // 2] or 1
    contact_text = " ".join(norm(b.text) for b in first_page if _CONTACT.search(b.text)).replace(" ", "")
    everything = [_squash(b.text) for b in boxes]
    candidates = []            # (score, name, box)
    for b in first_page:
        rel = (b.y0 - top) / span
        if id(b) in customer_boxes or rel > 0.45:
            continue
        name = b.text.strip(" ,")
        bonus = 0.0
        # A sender line above the address window: "Name - Street 1 - 12345 Town".
        parts = [x.strip() for x in re.split(r"\s+[-–—]\s+|\s*[|·•]\s*", b.text) if x.strip()]
        titled = _TITLE_AFTER_NAME.match(b.text.strip())
        if len(parts) >= 3 and looks_like_name(parts[0]) and any(_ADDRESS.search(x) for x in parts[1:]):
            name, bonus = parts[0], 2.5
        elif titled and looks_like_name(titled.group(1)):
            name = titled.group(1).strip()        # "Amazon Web Services Invoice": the name, then the title
        elif not looks_like_name(b.text) or _is_any_label(b):
            continue
        elif _OTHER_DATE.search(norm(b.text)) or re.search(r"(?i)datum\b|\bdate\b", b.text):
            continue
        elif sum(1 for o in first_page if o is not b and _same_line(b, o) and len(o.text) < 25) >= 3 \
                and (not b.size or b.size < body_size * 1.2):
            continue               # one heading in a row of column headings
        elif _is_key(b, first_page):
            continue               # "Währung   : EUR": the left half of a pair, not a name
        if company and similar(name, company) > 0.85:
            continue
        score = (1 - rel) * 2 + bonus
        if b.size:
            score += min(b.size / body_size, 2.5)
        tokens = [w for w in norm(name).split() if len(w) > 2 and w not in ("inc", "llc", "ltd", "the", "and", "corp")]
        if tokens and any(t in contact_text for t in tokens):
            score += 1.5           # the name also appears in its email address or website
        if has_legal_form(name):
            score += 1.0           # "... GmbH", "... B.V.", "... SARL": a company, not a heading
        key = _squash(name)
        if len(key) >= 4:          # printed again elsewhere (the footer, the payment details)
            score += min(sum(key in other for other in everything) - 1, 2) * 0.8
        candidates.append((score, name, b))
    if explicit:
        explicit_box = explicit[3]
        candidates = [c for c in candidates if c[2] is not explicit_box]
        same = [c for c in candidates if similar(c[1], explicit[0]) > 0.9]
        if same:   # the name at the top and the payee agree: as sure as it gets
            return same[0][1], 0.95, same[0][2].text
        if explicit[1] >= 0.85 and not looks_like_person(explicit[0]) and explicit[5] is not explicit[3]:
            return explicit[0], explicit[1], explicit[2]      # "Verkäufer:" with the name under it
    if not candidates:
        return (explicit[0], explicit[1], explicit[2]) if explicit else None
    candidates.sort(key=lambda c: -c[0])
    score, name, b = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0
    confidence = 0.55 + min(0.3, (score - runner_up) / 6)
    if explicit:
        # Checks are payable to a person; the business name is at the top. The business is the
        # vendor, but paying an individual is worth a second look.
        ex.warnings.append(f"Payment is to be made to \"{explicit[0]}\", not to {name}. "
                           "Check that the vendor really asks for that.")
    return name, round(confidence, 2), b.text


_TITLE_AFTER_NAME = re.compile(r"(?i)^(.{3,60}?\S)\s+(?:tax invoice|invoice|rechnung|facture|factuur|factura|fattura)$")
_SELLER_HEADINGS = {_plain(norm(label.rstrip(":"))) for label, _ in lang.LABELS["payable_to"]} | {
    "seller", "vendor", "supplier", "from", "issuer", "sender"}
_NAME_KEYS = {"name", "firma", "firmenname", "nom", "raison sociale", "naam", "bedrijfsnaam", "nombre",
              "razon social", "nome", "ragione sociale", "denominazione", "company", "company name"}


def _seller_section(boxes: list[Box]) -> Box | None:
    """The seller's name from a section headed "Verkäufer" / "Seller" / "Fournisseur": the value
    of a "Name:" line in it, or else the first line in it that reads like a name."""
    for head in sorted(boxes, key=lambda b: (b.page, b.y0)):
        if _plain(norm(head.text)) not in _SELLER_HEADINGS:
            continue
        below = sorted((b for b in boxes if b.page == head.page and head.y1 - 1 <= b.y0 <= head.y1 + head.height * 14
                        and b.x0 >= head.x0 - head.height), key=lambda b: (b.y0, b.x0))
        first_name = None
        for b in below:
            m = match_label(b.text, "customer")
            if (m and m[0] >= 2) or (_plain(norm(b.text)) in _SELLER_HEADINGS):
                break              # the next section (the buyer) starts here
            key = _plain(norm(b.text.split(":")[0]))
            if key in _NAME_KEYS:
                after = b.text.split(":", 1)[1].strip() if ":" in b.text else ""
                if after and looks_like_name(after):
                    return Box(after, b.x0, b.y0, b.x1, b.y1, b.page, b.size)
                right = sorted((o for o in boxes if o is not b and _same_line(b, o) and o.x0 >= b.x1 - 2),
                               key=lambda o: o.x0)
                if right and looks_like_name(right[0].text.lstrip(": ")):
                    o = right[0]
                    return Box(o.text.lstrip(": "), o.x0, o.y0, o.x1, o.y1, o.page, o.size)
            if first_name is None and abs(b.x0 - head.x0) < head.height * 2 and looks_like_name(b.text) \
                    and not _is_any_label(b) and not _is_key(b, boxes):
                first_name = b
        if first_name is not None:
            return first_name
    return None


def _is_key(box: Box, boxes: list[Box]) -> bool:
    """Is this the left half of a "key : value" pair (the next thing on its line starts with a colon)?"""
    right = [o for o in boxes if o is not box and _same_line(box, o) and o.x0 >= box.x1 - 1]
    return bool(right) and min(right, key=lambda o: o.x0).text.lstrip().startswith(":")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(text))


# ------------------------------------------------------------------ line items
def line_items_total(boxes: list[Box], subtotal_box: Box | None, total_box: Box | None) -> float | None:
    """Sum of the line-item amounts: the numbers in the "Amount"/"Line total" column,
    between its header and the subtotal (or total) row."""
    headers = [b for b in boxes if norm(b.text) in ("amount", "line total", "total", "total price", "ext price",
                                                   "extended price", "net amount", "price total", "amount usd",
                                                   "amount $", "line amount", "value")]
    end_box = subtotal_box or total_box
    # Left to right, a table reads quantity ... price ... line total: of two candidate headings
    # in one row ("Amount" for the quantity, "Total" for the line total) the right-hand one counts.
    for header in sorted(headers, key=lambda b: (b.page, round(b.y0 / 4), -b.x0)):
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
    order = _DAY_FIRST.set(date_order_hint(boxes))
    try:
        return _find_fields(ex, boxes, company)
    finally:
        _CREDIT_NOTE.reset(token)
        _DAY_FIRST.reset(order)


_CREDIT_TITLE = re.compile(r"(?i)^\s*(tax |vat )?(credit (note|memo|memorandum|invoice)|creditnote|avoir|"
                           r"gutschrift(?!sverfahren| \(selbst)|rechnungskorrektur|stornorechnung|note de cr[eé]dit|"
                           r"facture d'avoir|nota de cr[eé]dito|factura rectificativa|nota di credito|creditnota|"
                           r"creditfactuur)\b")


def is_credit_note(boxes: list[Box]) -> bool:
    """A credit note says so in its title, or labels its number "Credit note no."."""
    first = [b for b in boxes if b.page == 0]
    if not first:
        return False
    top = min(b.y0 for b in first)
    span = max(max(b.y1 for b in first) - top, 1)
    return any(_CREDIT_TITLE.match(b.text) and (b.y0 - top) / span < 0.5 for b in first)


# On a credit note: the phrase that names the invoice it corrects, in the six languages.
_CREDIT_REF = re.compile(
    r"(?i)(?:original invoice|invoice reference|referenced invoice|related invoice|applies to(?: invoice)?|"
    r"credit(?: note)? (?:for|against|on|to) invoice|corrects invoice|cancels invoice|relat(?:es|ing|ed) to invoice|in respect of invoice|"
    r"(?:zu|zur|für|bezüglich|betr\.?|bezug:?)\s+(?:unserer\s+|der\s+)?rechnung|ursprungsrechnung|bezugsrechnung|"
    r"ursprüngliche rechnung|rechnungsbezug|korrektur (?:zu|der) rechnung|storno (?:zu|der) rechnung|"
    r"facture d'origine|facture initiale|facture d'origine n|(?:sur|de) (?:la )?facture|avoir sur facture|"
    r"oorspronkelijke factuur|(?:op|bij) factuur|betreft factuur|creditering van factuur|"
    r"factura (?:rectificada|original|de origen)|fattura (?:di riferimento|originale|collegata|n\. rif))"
    r"(?:\s*(?:nr|no|n°|nº|#|number|nummer|numéro|numero|num))?\.?\s*[:.]?\s*(\S[^,;]{0,40})?$")


def _credit_reference(ex: Extraction, boxes: list[Box]) -> None:
    """Which invoice a credit note says it corrects, and make sure that number isn't taken
    for the credit note's own number."""
    for box in boxes:
        if len(box.text) > MAX_VALUE_TEXT:
            continue
        m = _CREDIT_REF.search(box.text)
        if not m:
            continue
        value, _ = parse_value("id", m.group(1)) if m.group(1) else (None, 0)
        if value is None and not m.group(1):
            value = find_value(boxes, box, "id", "")[0]
        if value:
            ex.fields["reference"], ex.confidence["reference"], ex.source["reference"] = str(value), 0.85, box.text
            break
    ref = ex.fields.get("reference")
    if ref and ex.fields.get("id") and _squash(str(ex.fields["id"])) == _squash(ref):
        # The number found was the original invoice's. Look for the credit note's own number
        # among the other labels.
        others = [b for b in boxes if _squash(ref) not in _squash(b.text)]
        hit = best(others, "id")
        if hit:
            ex.fields["id"], ex.confidence["id"], ex.source["id"] = hit[0], hit[1], hit[2]
        else:
            del ex.fields["id"]
            ex.confidence.pop("id", None)
            ex.source.pop("id", None)


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

    if ex.kind == "credit":
        _credit_reference(ex, boxes)

    total = best(boxes, "amount", prefer_low=True)
    subtotal = best(boxes, "subtotal", prefer_low=True)
    if total:
        ex.fields["amount"], ex.confidence["amount"], ex.source["amount"] = total[0], total[1], total[2]
    for kind in ("subtotal", "tax", "shipping", "discount", "paid"):
        hit = subtotal if kind == "subtotal" else best(boxes, kind, prefer_low=True)
        if hit:
            ex.totals[kind] = hit[0]
    printed = set()
    for box in boxes:                   # every total printed on the page, not only the one chosen
        m = match_label(box.text, "amount")
        value = find_value(boxes, box, "amount", m[1])[0] if m and m[0] >= 2 else None
        if value is not None and total and abs(value - total[0]) > 0.005:
            printed.add(value)
    if total and printed and "subtotal" in ex.totals and not _BALANCE_LABEL.search(norm(total[3].text)):
        # The page's own arithmetic picks the total: if subtotal + tax equals one of the other
        # printed totals and not the one chosen, the chosen one was something else (a line, a
        # carried-over figure). A label that says "to pay" / "balance due" is left alone: that
        # is what remains after a part payment.
        parts = {k: v for k, v in ex.totals.items() if k not in ("printed_totals", "tax_unread")}
        fitting = [v for v in printed if totals_issue(v, parts) is None and abs(
            v - (parts["subtotal"] + parts.get("tax", 0) + parts.get("shipping", 0) - parts.get("discount", 0))) <= 0.015]
        if fitting and totals_issue(total[0], parts) is not None and parts.get("tax"):
            printed.add(total[0])
            printed.discard(fitting[0])
            ex.fields["amount"], ex.confidence["amount"] = fitting[0], 0.9
            ex.source["amount"] = f"{fitting[0]:,.2f} (subtotal + tax)"
    if printed:
        ex.totals["printed_totals"] = sorted(printed)
    lines = line_items_total(boxes, subtotal[3] if subtotal else None, total[3] if total else None)
    if lines is not None:
        ex.totals["lines"] = lines
    if "tax" not in ex.totals and any(re.search(r"(?i)\b(vat|tax|mwst|ust|tva|btw|iva|gst)\b", b.text) for b in boxes):
        ex.totals["tax_unread"] = True      # tax is mentioned, but its amount wasn't found
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


_BALANCE_LABEL = re.compile(r"\b(due|balance|to pay|please pay|payable|a payer|zu zahlen|zahlbetrag|zahlbar|"
                            r"te betalen|a pagar|da pagare|a pagare|solde|reste)\b")
LABEL_NAMES = {"id": "Invoice number", "vendor": "Vendor", "date": "Invoice date", "amount": "Total amount",
               "bank_account": "Bank account"}


def totals_issue(amount: float | None, totals: dict) -> str | None:
    """A sentence if the invoice's own arithmetic doesn't add up, else None.

    Checks two things a printed invoice promises: the line items add up to the subtotal,
    and subtotal + tax + shipping (- discount) equals the total. Several readings are
    allowed (discount already taken off, or not; deposit already paid, or not), so only a
    real inconsistency is reported.

    A wrong warning here becomes a wrong flag on the invoice, so the check stays silent
    whenever the parts it would need were not read reliably: a "tax" bigger than any tax
    rate allows, no tax line found at all, or a total elsewhere on the page that does add
    up (the amount read is then a balance after a part payment).
    """
    if totals.get("embedded_conflict"):
        return totals["embedded_conflict"]     # the page and the PDF's own e-invoice data disagree
    sub, lines = totals.get("subtotal"), totals.get("lines")
    if amount is None or sub is None:
        return None
    tax = totals.get("tax", 0)
    if sub <= 0 or tax > sub * 0.35 + 0.01:
        return None                     # that isn't this invoice's tax: nothing to check against
    extras = tax + totals.get("shipping", 0)
    disc, paid = totals.get("discount", 0), totals.get("paid", 0)
    readings = {sub + extras, sub + extras - disc, sub + extras - paid, sub + extras - disc - paid,
                sub, sub - disc}

    def fits(value) -> bool:
        return any(abs(r - value) <= 0.015 for r in readings)

    printed = [v for v in totals.get("printed_totals", ()) if fits(v)]
    if fits(amount) or (printed and amount <= max(printed) + 0.015):
        if lines is not None and abs(lines - sub) > 0.015 and lines > 0 and abs(lines - disc - sub) > 0.015:
            return f"The line items add up to {lines:,.2f}, but the subtotal says {sub:,.2f}."
        return None
    if "tax" not in totals and "shipping" not in totals and amount > sub:
        rate = (amount - sub) / sub * 100
        if 2 <= rate <= 27 and (totals.get("tax_unread") or abs(rate - round(rate * 2) / 2) <= 0.05):
            return None                 # the difference is a tax (20%, 7.5%...) whose line wasn't read
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
    if einvoice.looks_like_xml(data):
        return "xml"
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
            gap = w["x0"] - current[-1]["x1"]
            if abs(gap) < size * 0.08:
                # No gap at all: one word that the PDF stores in two pieces ("4" + "71102").
                current[-1] = {**current[-1], "text": current[-1]["text"] + w["text"], "x1": w["x1"],
                               "bottom": max(current[-1]["bottom"], w["bottom"])}
            elif gap <= size * 0.75:    # a space is 0.25-0.6 of the font size (widest in typewriter fonts)
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
                # x_tolerance: how close two letters must be to count as one word. The default (3
                # points) glues the words of small or tightly set text together.
                words = page.extract_words(extra_attrs=["size"], keep_blank_chars=False, use_text_flow=False,
                                           x_tolerance=1.5)
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


def read_document(filename: str, data: bytes, company: str | None = None, embedded: bool = True,
                  hints: dict | None = None) -> Extraction:
    """The whole local pipeline for one file. Raises FileProblem for unusable files.

    embedded=False ignores e-invoice data inside a PDF and reads only the page (the real-invoice
    benchmark uses it, since that data is where its right answers come from).
    hints: what reviewers taught the reader, {canonical vendor: {field: [label keys]}}."""
    if not data:
        raise FileProblem("The file is empty.")
    if len(data) > MAX_BYTES:
        raise FileProblem(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    kind = kind_of(filename, data)
    if kind == "xml":
        parsed = einvoice.parse_xml(data)
        if not parsed or not parsed.get("id"):
            raise FileProblem("This XML file isn't an e-invoice this app can read (UBL, PEPPOL, XRechnung, "
                              "ZUGFeRD, Factur-X or FatturaPA).")
        return from_einvoice(parsed, company)
    if kind == "xlsx":
        boxes, method = boxes_from_xlsx(data), "Excel layout"
    elif kind == "pdf":
        boxes, method = boxes_from_pdf(data)
    elif kind == "image":
        boxes, method = boxes_from_image(data), "OCR (image)"
    else:
        raise FileProblem("Upload a PDF, an Excel invoice (.xlsx), or a photo or scan (.png, .jpg).")
    if len(boxes) > MAX_BOXES:
        raise FileProblem("This file has far more text than an invoice. Upload one invoice per file.")
    ex = find_fields(boxes, company, method)
    taught = (hints or {}).get(canonical_vendor(ex.fields.get("vendor") or ""))
    if taught:
        # This vendor's invoices were corrected before: read again, preferring the labels
        # the corrections pointed to.
        token = _LEARNED.set(taught)
        try:
            again = find_fields(boxes, company, method)
        finally:
            _LEARNED.reset(token)
        for f in ("id", "date", "amount"):
            source = again.source.get(f)
            if f in again.fields and again.fields.get(f) != ex.fields.get(f) and source is not None:
                again.learned[f] = next((c[8] for c in all_candidates(boxes, f) if c[8] in taught.get(f, ())
                                         and c[2] == again.fields[f]), "")
                again.warnings.append(f"The {LABEL_NAMES[f].lower()} was read where you corrected it on an "
                                      f"earlier {again.fields.get('vendor')} invoice.")
        ex = again
    ex.candidates = page_candidates(boxes)
    if method.startswith("OCR"):
        for f in ex.confidence:   # OCR can misread characters: always worth a look
            ex.confidence[f] = round(min(ex.confidence[f], 0.8), 2)
    if kind == "pdf" and embedded:
        parsed = einvoice.from_pdf(data)
        if parsed:
            return compare_with_page(from_einvoice(parsed, company), ex, boxes)
    return ex


def canonical_vendor(name: str) -> str:
    """A vendor name without formatting: lower case, no punctuation, no legal suffix."""
    words = re.sub(r"[^a-z0-9 ]+", " ", norm(name)).split()
    while words and words[-1] in ("inc", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh"):
        words.pop()
    return " ".join(words)


def page_candidates(boxes: list[Box], per_field: int = 6) -> dict:
    """The labelled values on the page for the fields people correct most, kept with the
    draft so a correction can be traced back to the label the right value was printed beside."""
    out = {}
    for f in ("id", "date", "amount"):
        seen, rows = set(), []
        for c in all_candidates(boxes, f):
            value, key = c[2], c[8]
            if key and (key, str(value)) not in seen and len(key) >= 3:
                seen.add((key, str(value)))
                rows.append([key, value])
            if len(rows) >= per_field:
                break
        out[f] = rows
    return out


def from_einvoice(data: dict, company: str | None = None) -> Extraction:
    """An Extraction from e-invoice data: exact values, read from the data, not the page."""
    fmt = getattr(data, "format", "") or "e-invoice"
    ex = Extraction(method=f"E-invoice data ({fmt})", kind=data.get("kind", "invoice"))
    for f in ("id", "vendor", "date", "amount"):
        if data.get(f) not in (None, ""):
            ex.fields[f] = data[f]
    account = data.get("bank_account")
    if account and (iban_ok(account) or account_value(account)):
        ex.fields["bank_account"] = re.sub(r"\s+", "", account).upper() if iban_ok(account) else account_value(account)
    for f in ex.fields:
        ex.confidence[f] = 0.99
        ex.source[f] = f"e-invoice data ({fmt})"
    ex.totals = dict(getattr(data, "totals", {}) or {})
    if data.get("amount_due") is not None and data.get("amount") is not None \
            and abs(data["amount_due"] - data["amount"]) > 0.005:
        ex.totals["paid"] = round(data["amount"] - data["amount_due"], 2)
        ex.warnings.append(f"Of the total {data['amount']:,.2f}, {data['amount_due']:,.2f} is still to pay "
                           "(the rest was paid in advance).")
    ex.customer, ex.currency = data.get("customer"), data.get("currency")
    if data.get("batch"):
        ex.warnings.append(f"This file holds {data['batch']} invoices; only the first was read. "
                           "Upload them one per file.")
    if data.get("reference"):
        ex.fields["reference"] = str(data["reference"])[:60]
    if ex.kind == "credit":
        ex.warnings.append("This is a credit note: it reduces what is owed to the vendor."
                           + (f" It refers to invoice {ex.fields['reference']}." if ex.fields.get("reference") else ""))
    if company and ex.fields.get("vendor") and similar(ex.fields["vendor"], company) > 0.85:
        ex.warnings.append(f"The seller in this e-invoice is {ex.fields['vendor']}, which is your own company. "
                           "Is this an invoice you sent rather than one you received?")
    return ex


def _amount_spellings(value: float) -> set[str]:
    """How an amount can be printed: 1589.8 -> 1589.80, 1,589.80, 1.589,80, 1589,80, 1 589,80."""
    whole, cents = f"{abs(value):.2f}".split(".")
    grouped = f"{int(whole):,}"
    return {f"{whole}.{cents}", f"{whole},{cents}", f"{grouped}.{cents}",
            f"{grouped.replace(',', '.')},{cents}", f"{grouped.replace(',', '')},{cents}",
            f"{grouped.replace(',', chr(39))}.{cents}"}


def compare_with_page(data_ex: Extraction, page: Extraction, boxes: list | None = None) -> Extraction:
    """Check the e-invoice data against what a person sees on the page.

    They should be the same invoice. If the page clearly shows a different total or number
    (read with high confidence, not a doubtful reading), one of the two has been changed
    after the invoice was made: the classic way to slip a different amount or bank account
    past someone who only looks at the page. That becomes a warning and a flag."""
    conflicts = []
    shown = {"amount": "total", "id": "invoice number", "bank_account": "bank account"}
    text = " ".join(str(b.text) for b in boxes or ())
    squashed = re.sub(r"\s+", "", text).upper()
    for f, label in shown.items():
        mine, theirs = data_ex.fields.get(f), page.fields.get(f)
        if mine is None or theirs is None or page.confidence.get(f, 0) < 0.9:
            continue
        # If the data's value is printed anywhere on the page, the page agrees and the reader
        # merely picked another figure. A changed page doesn't show the real value at all.
        if f == "amount":
            values = [mine, mine - data_ex.totals.get("paid", 0)]
            if any(sp in squashed for v in values for sp in _amount_spellings(v)):
                continue
        elif re.sub(r"\s+", "", str(mine)).upper() in squashed:
            continue
        if f == "amount":
            due = data_ex.fields["amount"] - data_ex.totals.get("paid", 0)
            same = any(abs(float(theirs) - v) < 0.005 for v in (mine, due))
            show = lambda v: f"{float(v):,.2f}"   # noqa: E731
        else:
            same = _squash(str(mine)) == _squash(str(theirs))
            show = str
        if not same:
            conflicts.append(f"the page shows {label} {show(theirs)}, but the e-invoice data in the file says {show(mine)}")
    if conflicts:
        message = ("This PDF contradicts itself: " + "; ".join(conflicts) + ". One of them has been changed. "
                   "Confirm the invoice with the vendor before paying.")
        data_ex.warnings.insert(0, message)
        data_ex.totals["embedded_conflict"] = message
    data_ex.pages = page.pages
    return data_ex


def document_text(filename: str, data: bytes) -> str:
    """Plain text of an Excel invoice laid out row by row (for the optional AI step)."""
    boxes = boxes_from_xlsx(data)
    rows: dict[float, list[Box]] = {}
    for b in boxes:
        rows.setdefault(b.y0, []).append(b)
    return "\n".join(" | ".join(b.text for b in sorted(r, key=lambda b: b.x0)) for _, r in sorted(rows.items()))
