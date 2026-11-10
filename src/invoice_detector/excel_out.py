"""Excel output: the user's own workbook with results written in, a formatted review
report, and a blank import template.

Two safety rules apply to every cell this module writes:
- Text is always stored as text. A value such as "=HYPERLINK(...)" from a vendor name
  would otherwise become a live formula when the workbook is opened (formula injection).
- The original workbook's cells are never changed. Results go into new columns after the
  last used column, and new sheets, so the user's data and formulas stay as they were.
"""
import io
from datetime import datetime, timezone

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .detection import RULE_INFO
from .importer import Table

RED = PatternFill("solid", fgColor="FDE2E0")
AMBER = PatternFill("solid", fgColor="FFF1D6")
GREY = PatternFill("solid", fgColor="EEF0F3")
GREEN = PatternFill("solid", fgColor="E3F4EA")
HEAD = PatternFill("solid", fgColor="1F3A8A")
HEAD_FONT = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")
STATUS_TEXT = {"open": "Needs review", "confirmed": "Confirmed problem", "dismissed": "False alarm"}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM = "application/vnd.ms-excel.sheet.macroEnabled.12"


def safe(value):
    """Neutralise text that Excel would treat as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def put(ws, row: int, col: int, value, **style):
    cell = ws.cell(row=row, column=col)
    cell.value = safe(value)
    if isinstance(cell.value, str):
        cell.data_type = "s"  # belt and braces: never a formula
    for key, val in style.items():
        setattr(cell, key, val)
    return cell


def risk_fill(risk: int | None) -> PatternFill | None:
    if risk is None:
        return None
    return RED if risk >= 70 else AMBER if risk >= 40 else GREY


def label(rule: str) -> str:
    return RULE_INFO.get(rule, (rule, ""))[0]


def _unique_title(wb, title: str) -> str:
    name, n = title, 2
    while name in wb.sheetnames:
        name = f"{title} {n}"
        n += 1
    return name


def _header_row(ws, row: int, headers: list[str], start_col: int = 1, widths=None) -> None:
    for i, text in enumerate(headers):
        put(ws, row, start_col + i, text, fill=HEAD, font=HEAD_FONT, alignment=WRAP)
        if widths:
            ws.column_dimensions[get_column_letter(start_col + i)].width = widths[i]


def _flagged_sheet(wb, items: list[dict], title: str = "Flagged invoices") -> None:
    ws = wb.create_sheet(_unique_title(wb, title))
    headers = ["Risk", "Invoice", "Vendor", "Amount", "Date", "Status", "Flags", "Why"]
    _header_row(ws, 1, headers, widths=[8, 16, 28, 14, 12, 18, 34, 80])
    for r, item in enumerate(items, start=2):
        values = [item["risk"], item["invoice_id"], item.get("vendor"), item.get("amount"), item.get("date"),
                  STATUS_TEXT.get(item.get("status", "open"), item.get("status")),
                  ", ".join(label(x) for x in item["rules"]), "; ".join(item["details"])]
        for c, value in enumerate(values, start=1):
            put(ws, r, c, value, alignment=WRAP)
        ws.cell(row=r, column=1).fill = risk_fill(item["risk"])
        ws.cell(row=r, column=4).number_format = "#,##0.00"
    ws.freeze_panes = "B2"
    if items:
        ws.auto_filter.ref = f"A1:H{len(items) + 1}"


def _about_sheet(wb, company: str, extra: list[tuple[str, str]] = ()) -> None:
    ws = wb.create_sheet(_unique_title(wb, "About these flags"))
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 90
    rows = [("Company", company),
            ("Created", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
            *extra,
            ("", ""),
            ("Important", "Flags are signals for human review, not proof of fraud or error."),
            ("Risk score", "0-100. Several weak signals add up, but never outrank one strong signal. "
                           "70+ is shaded red, 40-69 amber."),
            ("", ""),
            ("Flag", "Meaning")]
    rows += [(name, meaning) for name, meaning in RULE_INFO.values()]
    for r, (a, b) in enumerate(rows, start=1):
        put(ws, r, 1, a, font=BOLD, alignment=WRAP)
        put(ws, r, 2, b, alignment=WRAP)
    for c in (1, 2):
        ws.cell(row=len(rows) - len(RULE_INFO), column=c).fill = GREY


def _save(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ your file, annotated
def annotate(filename: str, data: bytes, table: Table, lines: dict[str, int], errors: list[dict],
             queue: dict[str, dict], company: str) -> tuple[bytes, str, str]:
    """Return (workbook bytes, download name, media type).

    lines:  invoice id -> spreadsheet row, for rows that were imported
    errors: rows that couldn't be imported, with the reason
    queue:  invoice id -> review-queue item (risk, rules, details, status, ...) for flagged invoices
    """
    lower = filename.lower()
    macro = lower.endswith(".xlsm")
    if lower.endswith((".xlsx", ".xlsm")):
        wb = load_workbook(io.BytesIO(data), keep_vba=macro)
        ws = wb[table.sheet]
    else:  # CSV: rebuild it as a worksheet first
        wb = Workbook()
        ws = wb.active
        ws.title = "Invoices"
        for c, head in enumerate(table.headers, start=1):
            put(ws, table.first_row_number - 1, c, head, font=BOLD)
        for i, row in enumerate(table.rows):
            for c, value in enumerate(row, start=1):
                put(ws, table.first_row_number + i, c, value)

    header = table.first_row_number - 1
    last = table.first_row_number + len(table.rows) - 1
    used = len(table.headers)
    for row in ws.iter_rows(min_row=header, max_row=max(header, last)):
        for cell in reversed(row):
            if cell.value is not None and str(cell.value).strip() != "":
                used = max(used, cell.column)
                break
    col = used + 1
    _header_row(ws, header, ["Risk", "Review status", "Flags", "Why flagged"], start_col=col,
                widths=[8, 18, 30, 70])

    problems = {e["row"]: e["message"] for e in errors}
    for line in range(table.first_row_number, last + 1):
        if line in problems:
            put(ws, line, col + 1, "Not imported", fill=GREY)
            put(ws, line, col + 3, problems[line], alignment=WRAP)
    for inv_id, line in lines.items():
        item = queue.get(inv_id)
        if item is None:
            put(ws, line, col + 1, "No flags", fill=GREEN)
            continue
        fill = risk_fill(item["risk"])
        put(ws, line, col, item["risk"], fill=fill, font=BOLD)
        put(ws, line, col + 1, STATUS_TEXT.get(item["status"], item["status"]), fill=fill)
        put(ws, line, col + 2, ", ".join(label(r) for r in item["rules"]), fill=fill, alignment=WRAP)
        put(ws, line, col + 3, "; ".join(item["details"]), alignment=WRAP)

    flagged = sorted((queue[i] for i in lines if i in queue), key=lambda x: (-x["risk"], x["invoice_id"]))
    _flagged_sheet(wb, flagged)
    _about_sheet(wb, company, [("Source file", filename), ("Sheet", table.sheet or "CSV"),
                               ("Invoices checked", str(len(lines))), ("Flagged", str(len(flagged))),
                               ("Rows not imported", str(len(errors)))])
    wb.active = wb.sheetnames.index(ws.title)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    ext = "xlsm" if macro else "xlsx"
    return _save(wb), f"{stem} - checked.{ext}", XLSM if macro else XLSX


# ------------------------------------------------------------------ reports
def review_report(items: list[dict], company: str, summary: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    _flagged_sheet(wb, items, "Review queue")
    _about_sheet(wb, company, [("Invoices stored", str(summary.get("invoices", ""))),
                               ("Flagged", str(summary.get("flagged", ""))),
                               ("Needs review", str(summary.get("open", ""))),
                               ("Confirmed problems", str(summary.get("confirmed", ""))),
                               ("False alarms", str(summary.get("dismissed", "")))])
    return _save(wb)


def template() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoices"
    _header_row(ws, 1, ["Invoice number", "Vendor", "Invoice date", "Amount", "Bank account"],
                widths=[18, 30, 14, 14, 32])
    example = ["INV-1001", "Example Supplies Ltd", datetime(2026, 1, 15), 1250.00, "GB33BUKB20201555555555"]
    for c, value in enumerate(example, start=1):
        put(ws, 2, c, value, font=Font(italic=True, color="6B7280"))
    ws.cell(row=2, column=3).number_format = "yyyy-mm-dd"
    ws.cell(row=2, column=4).number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    notes = wb.create_sheet("How to fill this in")
    notes.column_dimensions["A"].width = 100
    for r, text in enumerate([
        "One row per invoice. Replace the grey example row with your own.",
        "Required: invoice number, vendor, invoice date, amount. Bank account is optional but enables the bank checks.",
        "Dates: real Excel dates, or text like 2026-01-15. Amounts: numbers, with or without a currency symbol.",
        "Column names don't have to match exactly; you'll confirm which column is which when you upload.",
        "Upload the file on the 'Upload invoices' page of the web app.",
    ], start=1):
        put(notes, r, 1, text, alignment=WRAP)
    return _save(wb)
