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

from .decisions import DECISION_TEXT, fix_steps, payment_decision
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
DECISION_FILL = {"do_not_pay": RED, "hold": AMBER, "review": GREY, "pay": GREEN}
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


def decision_of(item: dict | None) -> str:
    return item.get("decision") or payment_decision(item) if item else "pay"


def _flagged_sheet(wb, items: list[dict], title: str = "Flagged invoices") -> None:
    ws = wb.create_sheet(_unique_title(wb, title))
    headers = ["Risk", "Invoice", "Vendor", "Amount", "Date", "Status", "Payment decision", "Flags",
               "Why flagged", "What to do", "Reviewer note"]
    _header_row(ws, 1, headers, widths=[8, 16, 28, 14, 12, 18, 16, 30, 60, 70, 30])
    for r, item in enumerate(items, start=2):
        decision = decision_of(item)
        values = [item["risk"], item["invoice_id"], item.get("vendor"), item.get("amount"), item.get("date"),
                  STATUS_TEXT.get(item.get("status", "open"), item.get("status")), DECISION_TEXT[decision],
                  ", ".join(label(x) for x in item["rules"]), "; ".join(item["details"]),
                  " ".join(item.get("fixes") or fix_steps(item["rules"])), item.get("note")]
        for c, value in enumerate(values, start=1):
            put(ws, r, c, value, alignment=WRAP)
        ws.cell(row=r, column=1).fill = risk_fill(item["risk"])
        ws.cell(row=r, column=7).fill = DECISION_FILL[decision]
        ws.cell(row=r, column=4).number_format = "#,##0.00"
    ws.freeze_panes = "C2"
    if items:
        ws.auto_filter.ref = f"A1:K{len(items) + 1}"


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
            ("Payment decision", "Pay: no flags, or flags dismissed as false alarms. Review first: flagged, "
                                 "not reviewed yet. Hold: a confirmed problem to check before paying. "
                                 "Do not pay: a confirmed problem such as a duplicate."),
            ("Your invoices", "Invoices are never changed or deleted. Decisions apply to the payment, "
                              "and the original record stays as the supplier sent it."),
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


ANNOTATION_HEADERS = ["Risk", "Review status", "Payment decision", "Flags", "Why flagged", "What to do"]


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
    _header_row(ws, header, ANNOTATION_HEADERS, start_col=col, widths=[8, 18, 16, 30, 60, 70])

    problems = {e["row"]: e["message"] for e in errors}
    for line in range(table.first_row_number, last + 1):
        if line in problems:
            put(ws, line, col + 1, "Not imported", fill=GREY)
            put(ws, line, col + 4, problems[line], alignment=WRAP)
            put(ws, line, col + 5, "Fix this row in your file and upload it again.", alignment=WRAP)
    for inv_id, line in lines.items():
        item = queue.get(inv_id)
        if item is None:
            put(ws, line, col + 1, "No flags", fill=GREEN)
            put(ws, line, col + 2, DECISION_TEXT["pay"], fill=GREEN)
            continue
        fill = risk_fill(item["risk"])
        decision = decision_of(item)
        put(ws, line, col, item["risk"], fill=fill, font=BOLD)
        put(ws, line, col + 1, STATUS_TEXT.get(item["status"], item["status"]), fill=fill)
        put(ws, line, col + 2, DECISION_TEXT[decision], fill=DECISION_FILL[decision], font=BOLD)
        put(ws, line, col + 3, ", ".join(label(r) for r in item["rules"]), fill=fill, alignment=WRAP)
        put(ws, line, col + 4, "; ".join(item["details"]), alignment=WRAP)
        steps = " ".join(item.get("fixes") or fix_steps(item["rules"]))
        if item.get("note"):
            steps += f" Reviewer note: {item['note']}"
        put(ws, line, col + 5, steps, alignment=WRAP)

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


def payment_file(invoices: list[dict], queue: dict[str, dict], company: str) -> bytes:
    """The corrected payment batch: every invoice sorted into Ready to pay, Review first,
    On hold and Do not pay, with totals. invoices: id, vendor, amount, invoice_date."""
    groups: dict[str, list[tuple[dict, dict | None]]] = {d: [] for d in ("pay", "review", "hold", "do_not_pay")}
    for inv in invoices:
        item = queue.get(inv["id"])
        groups[decision_of(item)].append((inv, item))
    totals = {d: round(sum(inv["amount"] for inv, _ in rows), 2) for d, rows in groups.items()}

    wb = Workbook()
    summary = wb.active
    summary.title = "Summary"
    summary.column_dimensions["A"].width = 22
    summary.column_dimensions["B"].width = 12
    summary.column_dimensions["C"].width = 16
    summary.column_dimensions["D"].width = 70
    put(summary, 1, 1, f"Payment file - {company}", font=Font(bold=True, size=14))
    put(summary, 2, 1, "Created " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    _header_row(summary, 4, ["Decision", "Invoices", "Amount", "Meaning"])
    meaning = {"pay": "No problems found, or flags dismissed as false alarms. Safe to pay.",
               "review": "Flagged but not reviewed yet. Review in the app before paying.",
               "hold": "Confirmed problem. Check it (see What to do) before paying.",
               "do_not_pay": "Confirmed problem such as a duplicate. Do not pay."}
    for r, d in enumerate(("pay", "review", "hold", "do_not_pay"), start=5):
        put(summary, r, 1, DECISION_TEXT[d], fill=DECISION_FILL[d], font=BOLD)
        put(summary, r, 2, len(groups[d]))
        put(summary, r, 3, totals[d]).number_format = "#,##0.00"
        put(summary, r, 4, meaning[d], alignment=WRAP)
    put(summary, 9, 1, "Total", font=BOLD)
    put(summary, 9, 2, len(invoices), font=BOLD)
    put(summary, 9, 3, round(sum(totals.values()), 2), font=BOLD).number_format = "#,##0.00"
    put(summary, 11, 1, "Note", font=BOLD)
    put(summary, 11, 4, "Invoices are not changed. This file sorts them by what should happen to the payment. "
                        "Run detection and finish reviews in the app before using it for a payment run.",
        alignment=WRAP)
    credits = [inv for inv in invoices if inv.get("kind") == "credit"]
    if credits:
        put(summary, 12, 4, f"Includes {len(credits)} credit note{'s' if len(credits) != 1 else ''} "
                            f"(shown with negative amounts, {round(-sum(c['amount'] for c in credits), 2):,.2f} in all): "
                            "totals are net, so a credit in Ready to pay reduces what that vendor is paid.",
            alignment=WRAP)

    titles = {"pay": "Ready to pay", "review": "Review first", "hold": "On hold", "do_not_pay": "Do not pay"}
    for d, rows in groups.items():
        ws = wb.create_sheet(titles[d])
        headers = ["Invoice", "Type", "Vendor", "Date", "Amount", "Risk", "Flags", "What to do", "Reviewer note"]
        _header_row(ws, 1, headers, widths=[16, 12, 28, 12, 14, 8, 30, 70, 30])
        for r, (inv, item) in enumerate(rows, start=2):
            values = [inv["id"], "Credit note" if inv.get("kind") == "credit" else "Invoice", inv["vendor"],
                      inv["invoice_date"], inv["amount"],
                      item["risk"] if item else None,
                      ", ".join(label(x) for x in item["rules"]) if item else None,
                      " ".join(item.get("fixes") or fix_steps(item["rules"])) if item and d != "pay" else None,
                      item.get("note") if item else None]
            for c, value in enumerate(values, start=1):
                put(ws, r, c, value, alignment=WRAP)
            ws.cell(row=r, column=5).number_format = "#,##0.00"
        total_row = len(rows) + 2
        put(ws, total_row, 1, "Total (net of credit notes)" if any(i.get("kind") == "credit" for i, _ in rows) else "Total",
            font=BOLD)
        put(ws, total_row, 5, totals[d], font=BOLD).number_format = "#,##0.00"
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = f"A1:I{len(rows) + 1}"
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
        "Credit notes: enter the amount as a negative number (-120.00), or add a Type column that says Credit note.",
        "Column names don't have to match exactly; you'll confirm which column is which when you upload.",
        "Upload the file on the 'Upload invoices' page of the web app.",
    ], start=1):
        put(notes, r, 1, text, alignment=WRAP)
    return _save(wb)
