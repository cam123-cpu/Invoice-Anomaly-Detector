"""Read invoice exports from Excel (.xlsx) or CSV, suggest a column mapping, and turn
rows into clean invoices with a clear error for every row that can't be used.

Real exports are messy: title rows above the header, "Invoice #" instead of "id",
"$1,234.50" or "1.234,50", dates as Excel serial numbers or as 05/01/2026 (which could
be 5 January or May 1st). This module deals with that, and never guesses silently
where a wrong guess would corrupt data: an ambiguous date order is reported so the
user can choose.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

FIELDS = ("id", "vendor", "amount", "date", "bank_account", "kind", "reference")
REQUIRED = ("id", "vendor", "amount", "date")
FIELD_LABELS = {"id": "Invoice number", "vendor": "Vendor", "amount": "Amount",
                "date": "Invoice date", "bank_account": "Bank account (optional)",
                "kind": "Type: invoice or credit note (optional)",
                "reference": "Credit notes: the invoice they apply to (optional)"}
MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 50_000
MAX_UNZIPPED = 200 * 1024 * 1024   # an .xlsx is a zip; refuse ones that expand to more than this
MAX_ZIP_ENTRIES = 2_000
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./#:-]{0,63}$")

# Normalized header names (lowercase letters and digits only) for each field.
SYNONYMS = {
    "id": ["id", "invoice", "invoiceid", "invoiceno", "invoicenumber", "invoicenum", "invno", "num",
           "invnumber", "invoiceref", "invoicereference", "documentno", "documentnumber", "docno",
           "docnumber", "billno", "billnumber", "reference", "ref", "number", "no"],
    "vendor": ["vendor", "vendorname", "supplier", "suppliername", "payee", "payeename",
               "merchant", "creditor", "company", "companyname", "name", "seller"],
    "amount": ["amount", "total", "invoicetotal", "totalamount", "amountdue", "invoiceamount",
               "grossamount", "gross", "totalgross", "netamount", "amountpayable", "value",
               "totalinclvat", "totalincltax", "balance", "sum"],
    "date": ["date", "invoicedate", "billdate", "documentdate", "docdate", "issuedate",
             "dateissued", "postingdate", "transactiondate", "txndate", "invdate"],
    "bank_account": ["bankaccount", "bankaccountnumber", "bankacct", "iban", "payeeaccount",
                     "remittanceaccount", "vendorbankaccount", "supplierbankaccount",
                     "accountiban", "bankdetails"],
    "kind": ["type", "documenttype", "doctype", "invoicetype", "transactiontype", "txntype", "trantype",
             "recordtype", "kind"],
    # Only names that can't mean the row's own number: "Reference" alone often is.
    "reference": ["appliesto", "appliestoinvoice", "originalinvoice", "originalinvoiceno", "originalinvoicenumber",
                  "relatedinvoice", "creditedinvoice", "correctsinvoice", "creditforinvoice", "againstinvoice",
                  "bezugsrechnung", "ursprungsrechnung", "factureorigine", "facturedorigine", "oorspronkelijkefactuur"],
}

# What a "Type" column says when the row is a credit note.
_CREDIT_WORDS = re.compile(r"(?i)\b(credit|cn|cr|crn|credit ?note|credit ?memo|creditmemo|refund|gutschrift|avoir)\b")


def is_credit_text(value) -> bool:
    return bool(_CREDIT_WORDS.search(str(value or "").replace("_", " ").replace("-", " ")))
_ALL_SYNONYMS = {s for names in SYNONYMS.values() for s in names}


class FileProblem(ValueError):
    """The file can't be read at all. The message is safe to show to users."""


@dataclass
class Table:
    headers: list[str]
    rows: list[list]                     # raw cell values: str, int, float, datetime or None
    first_row_number: int = 2            # spreadsheet row number of rows[0], for messages
    sheet: str | None = None
    sheets: list[str] = field(default_factory=list)  # every sheet that has data (Excel only)


@dataclass
class Converted:
    invoices: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)  # {"row": n, "message": "..."}
    blank_rows: int = 0
    lines: dict[str, int] = field(default_factory=dict)  # invoice id -> spreadsheet row number


def normalize_header(value) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


# ------------------------------------------------------------------ reading files
def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")  # older Windows Excel CSV exports


def _read_csv(data: bytes) -> list[list]:
    text = _decode(data)
    sample = text[:20_000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _sheet_rows(ws) -> list[list]:
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append(list(row))
        if len(rows) > MAX_ROWS + 20:
            break
    return rows


def _has_data(rows: list[list]) -> bool:
    return sum(1 for r in rows if sum(_present(v) for v in r) >= 2) >= 2


def _check_xlsx(data: bytes) -> None:
    """An .xlsx file is a zip archive. Check that before handing it to the Excel library, and
    refuse 'zip bombs': small files that expand to gigabytes and exhaust memory."""
    import zipfile
    if not data.startswith(b"PK\x03\x04"):
        raise FileProblem("This file is named .xlsx but isn't an Excel workbook. "
                          "Open it in Excel and save it as .xlsx or .csv.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries = z.infolist()
    except zipfile.BadZipFile:
        raise FileProblem("This Excel file is damaged. Re-save it as .xlsx or .csv.") from None
    if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_UNZIPPED:
        raise FileProblem("This Excel file is too large to read safely. Split it into smaller files.")
    if not any(e.filename == "xl/workbook.xml" for e in entries):
        raise FileProblem("This file is named .xlsx but isn't an Excel workbook.")


def _check_text(data: bytes) -> None:
    """A CSV file is text. Refuse binary files (images, programs, renamed workbooks)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 text has zero bytes; that's fine
        return
    if b"\x00" in data[:65536] or data.startswith(b"PK\x03\x04"):
        raise FileProblem("This file is named .csv but isn't text. If it's an Excel file, "
                          "give it the .xlsx ending or save it as CSV from Excel.")


def _read_xlsx(data: bytes, sheet: str | None = None) -> tuple[list[list], str | None, list[str]]:
    """Rows of the chosen sheet (or the first sheet with a table, active sheet first),
    its name, and the names of all sheets that contain a table."""
    from openpyxl import load_workbook

    _check_xlsx(data)

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # corrupt file, wrong format, password protected
        raise FileProblem("This Excel file could not be opened. If it is password-protected, "
                          "remove the password; otherwise re-save it as .xlsx or .csv.") from exc
    try:
        ordered = [wb.active] + [ws for ws in wb.worksheets if ws is not wb.active]
        tables = {}
        for ws in ordered:
            rows = _sheet_rows(ws)
            if _has_data(rows):
                tables[ws.title] = rows
        if sheet is not None:
            if sheet not in wb.sheetnames:
                raise FileProblem(f"There is no sheet called '{sheet}' in this workbook.")
            rows = tables.get(sheet) or _sheet_rows(wb[sheet])
            return rows, sheet, list(tables)
        if tables:
            name = next(iter(tables))
            return tables[name], name, list(tables)
        return [], None, []
    finally:
        wb.close()


def _present(value) -> bool:
    return value is not None and str(value).strip() != ""


def _find_header(rows: list[list]) -> int:
    """Index of the header row: among the first 15 rows, the one whose cells best match
    known column names. Skips report titles and blank lines above the real header."""
    best, best_score = None, 0
    for i, row in enumerate(rows[:15]):
        score = sum(normalize_header(v) in _ALL_SYNONYMS for v in row if _present(v))
        if score > best_score:
            best, best_score = i, score
    if best is not None and best_score >= 2:
        return best
    for i, row in enumerate(rows):
        if sum(_present(v) for v in row) >= 2:
            return i
    raise FileProblem("No header row found. The file needs a row of column names, "
                      "such as Invoice number, Vendor, Amount and Date.")


def read_table(filename: str, data: bytes, sheet: str | None = None) -> Table:
    if not data:
        raise FileProblem("The file is empty.")
    if len(data) > MAX_BYTES:
        raise FileProblem(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB. Split it into smaller files.")
    name = (filename or "").lower()
    sheets: list[str] = []
    if name.endswith((".xlsx", ".xlsm")):
        rows, sheet, sheets = _read_xlsx(data, sheet)
    elif name.endswith(".xls"):
        raise FileProblem("Old .xls files aren't supported. In Excel, use File > Save As and "
                          "choose .xlsx or .csv.")
    elif name.endswith((".csv", ".txt", ".tsv")):
        _check_text(data)
        rows, sheet = _read_csv(data), None
    else:
        raise FileProblem("Upload an Excel (.xlsx) or CSV (.csv) file.")

    header_at = _find_header(rows)
    raw_headers = rows[header_at]
    width = max(i + 1 for i, v in enumerate(raw_headers) if _present(v))
    headers, seen = [], {}
    for i in range(width):
        label = str(raw_headers[i]).strip() if i < len(raw_headers) and _present(raw_headers[i]) \
            else f"Column {i + 1}"
        count = seen.get(label, 0)
        seen[label] = count + 1
        headers.append(label if count == 0 else f"{label} ({count + 1})")

    body = [list(r[:width]) + [None] * (width - len(r[:width])) for r in rows[header_at + 1:]]
    while body and not any(_present(v) for v in body[-1]):
        body.pop()
    if len(body) > MAX_ROWS:
        raise FileProblem(f"The file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    return Table(headers, body, first_row_number=header_at + 2, sheet=sheet, sheets=sheets)


# ------------------------------------------------------------------ column mapping
def suggest_mapping(headers: list[str]) -> dict[str, str | None]:
    """Best-guess header for each field. Exact name matches win; then containment
    (e.g. 'Supplier Name (legal)'). Each header is used at most once."""
    normalized = {h: normalize_header(h) for h in headers}
    mapping: dict[str, str | None] = {f: None for f in FIELDS}
    used: set[str] = set()

    for field_name in FIELDS:                       # pass 1: exact, in synonym priority order
        for synonym in SYNONYMS[field_name]:
            match = next((h for h in headers if h not in used and normalized[h] == synonym), None)
            if match:
                mapping[field_name] = match
                used.add(match)
                break
    for field_name in FIELDS:                       # pass 2: containment, longer synonyms first
        if mapping[field_name]:
            continue
        for synonym in sorted(SYNONYMS[field_name], key=len, reverse=True):
            if len(synonym) < 4:
                continue
            match = next((h for h in headers if h not in used and synonym in normalized[h]), None)
            if match:
                mapping[field_name] = match
                used.add(match)
                break
    return mapping


# ------------------------------------------------------------------ values
_CURRENCY = re.compile(r"(?i)(us\$|usd|eur|gbp|cad|aud|chf|jpy|inr|[$€£¥₹])")
_NUMERIC_DATE = re.compile(r"^\s*(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})\s*$")
_TEXT_DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
                      "%d-%b-%Y", "%d-%b-%y", "%d %b %y")


def parse_amount(value, signed: bool = False) -> float:
    """A positive amount. With signed=True a negative amount is allowed and returned as
    negative: that's how most systems export credit notes (-120.00, (120.00), 120.00-)."""
    if isinstance(value, bool):
        raise ValueError("amount is not a number")
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("amount is missing")
        text = _CURRENCY.sub("", text).replace(" ", "").replace(" ", "").replace("'", "")
        negative = text.startswith("(") and text.endswith(")") or text.endswith("-") \
            or text.startswith("-")   # after the currency sign is gone: $-5 and -$5 both count
        text = text.strip("()+-")
        if "," in text and "." in text:
            decimal = "," if text.rfind(",") > text.rfind(".") else "."
            text = text.replace("." if decimal == "," else ",", "").replace(",", ".")
        elif "," in text:
            text = text.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", text) \
                else text.replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(\.\d{3}){2,}", text):
            text = text.replace(".", "")  # 1.234.567 (European thousands)
        try:
            amount = float(text)
        except ValueError:
            raise ValueError(f"amount '{value}' is not a number") from None
        if negative:
            amount = -amount
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise ValueError("amount is not a number")
    if amount == 0 or (amount < 0 and not signed):
        raise ValueError("amount must be greater than zero" if not signed else "amount is zero")
    if abs(amount) >= 1e12:
        raise ValueError("amount is unrealistically large")
    return round(amount, 2)


def detect_date_order(values) -> tuple[str, bool]:
    """('MDY' or 'DMY', ambiguous). Looks at every numeric date like 05/01/2026:
    a first number over 12 proves day-first, a second number over 12 proves month-first."""
    day_first = month_first = False
    for value in values:
        if not isinstance(value, str):
            continue
        m = _NUMERIC_DATE.match(value)
        if not m or len(m.group(1)) == 4:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        day_first |= a > 12
        month_first |= b > 12
    if day_first and not month_first:
        return "DMY", False
    if month_first and not day_first:
        return "MDY", False
    ambiguous = not (day_first or month_first) and any(
        isinstance(v, str) and _NUMERIC_DATE.match(v) and len(_NUMERIC_DATE.match(v).group(1)) != 4
        for v in values)
    return "MDY", ambiguous


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def parse_date(value, order: str = "MDY") -> str:
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not 20_000 <= value <= 80_000:
            raise ValueError(f"date '{value}' is not a date")
        d = date(1899, 12, 30) + timedelta(days=int(value))  # Excel serial number
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("date is missing")
        text = re.split(r"[T ]\d{1,2}:\d{2}", text)[0].strip()  # drop a time part
        m = _NUMERIC_DATE.match(text)
        try:
            if m:
                a, b, c = (int(x) for x in m.groups())
                if len(m.group(1)) == 4:
                    d = date(a, b, c)
                elif order == "DMY":
                    d = date(_year(c), b, a)
                else:
                    d = date(_year(c), a, b)
            else:
                for fmt in _TEXT_DATE_FORMATS:
                    try:
                        d = datetime.strptime(text, fmt).date()
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError
        except ValueError:
            raise ValueError(f"date '{value}' is not a recognised date") from None
    if not 1990 <= d.year <= 2100:
        raise ValueError(f"date '{value}' is outside 1990-2100")
    return d.isoformat()


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # 1001.0 from Excel -> "1001"
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat(" ")
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(str(value).split())


# ------------------------------------------------------------------ conversion
def validate_mapping(mapping: dict, headers: list[str]) -> dict[str, str | None]:
    clean = {}
    for field_name in FIELDS:
        header = mapping.get(field_name) or None
        if header is not None and header not in headers:
            raise FileProblem(f"Column '{header}' is not in the file.")
        if header is None and field_name in REQUIRED:
            raise FileProblem(f"Choose the column that holds: {FIELD_LABELS[field_name]}.")
        clean[field_name] = header
    chosen = [h for h in clean.values() if h]
    if len(chosen) != len(set(chosen)):
        raise FileProblem("Each column can only be used once.")
    return clean


def convert(table: Table, mapping: dict, date_order: str = "MDY") -> Converted:
    mapping = validate_mapping(mapping, table.headers)
    index = {f: table.headers.index(h) for f, h in mapping.items() if h}
    out = Converted()
    seen: dict[str, int] = {}
    for offset, row in enumerate(table.rows):
        line = table.first_row_number + offset
        cell = {f: row[i] for f, i in index.items()}
        if not _present(cell.get("id")) and not _present(cell.get("date")):
            out.blank_rows += 1  # blank line, or a totals / subtotal row
            continue
        try:
            inv_id = _text(cell["id"])
            if not inv_id:
                raise ValueError("invoice number is missing")
            if not ID_PATTERN.match(inv_id):
                raise ValueError(f"invoice number '{inv_id[:40]}' has unsupported characters "
                                 "(use letters, digits, spaces and _ . / # : -)")
            if inv_id in seen:
                raise ValueError(f"invoice number '{inv_id}' already appears on row {seen[inv_id]}")
            vendor = _text(cell["vendor"])
            if not vendor:
                raise ValueError("vendor is missing")
            # A credit note is a negative amount, or a row whose Type column says so. It is
            # stored as its own kind with a positive amount.
            amount = parse_amount(cell["amount"], signed=True)
            credit = amount < 0 or is_credit_text(cell.get("kind"))
            invoice = {"id": inv_id, "vendor": vendor[:120],
                       "amount": abs(amount),
                       "date": parse_date(cell["date"], date_order),
                       "bank_account": _text(cell.get("bank_account"))[:64] or None,
                       "kind": "credit" if credit else "invoice"}
            ref = _text(cell.get("reference"))[:64] if credit else ""
            if ref and ID_PATTERN.match(ref):
                invoice["reference"] = ref
        except ValueError as exc:
            out.errors.append({"row": line, "message": str(exc)})
            continue
        seen[inv_id] = line
        out.lines[inv_id] = line
        out.invoices.append(invoice)
    return out


def preview(table: Table, sample: int = 8) -> dict:
    mapping = suggest_mapping(table.headers)
    date_values = []
    if mapping["date"]:
        i = table.headers.index(mapping["date"])
        date_values = [r[i] for r in table.rows]
    order, ambiguous = detect_date_order(date_values)
    return {
        "sheet": table.sheet,
        "sheets": table.sheets,
        "headers": table.headers,
        "total_rows": len(table.rows),
        "mapping": mapping,
        "date_order": order,
        "date_order_ambiguous": ambiguous,
        "sample_rows": [[_text(v) for v in r] for r in table.rows[:sample]],
        "fields": [{"name": f, "label": FIELD_LABELS[f], "required": f in REQUIRED} for f in FIELDS],
    }
