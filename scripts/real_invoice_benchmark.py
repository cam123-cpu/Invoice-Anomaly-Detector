"""Measure invoice reading on real invoices nobody here designed.

    pip install pypdf            # only this script needs it
    python scripts/real_invoice_benchmark.py [--keep DIR] [--min-all-four 0.7]

Downloads two public collections at fixed commits, works out the right answers, and runs
`evaluate-documents --folder` on each:

  1. invoice2data's test invoices (MIT): 11 PDFs from real companies (AWS, a Dutch web
     shop, a French ISP, a German host...) with the answers in JSON next to them.
  2. The ZUGFeRD corpus (Apache 2.0): about 150 German and French e-invoice PDFs. Each
     carries its own data as embedded XML, which gives the right answers; the reader is
     scored on the page a person sees, never on the XML.

Nothing downloaded is added to this repository. The e-invoice vendors are split in two
(alphabetically, alternating): the reader was tuned on the first half and the invoice2data
set; the second half is reported separately.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from invoice_detector.doc_benchmark import format_report, score_folder

SOURCES = {
    "invoice2data": ("https://github.com/invoice-x/invoice2data.git", "379ddd43d0ab4d466fc7db1c5f9311e877f84889"),
    "corpus": ("https://github.com/ZUGFeRD/corpus.git", "84f8d8f68f6c3698aa3019d701a8b82db9ba0aac"),
}
COLUMNS = ("file", "id", "vendor", "date", "amount", "amount_due", "kind")


def fetch(name: str, into: Path) -> Path:
    url, commit = SOURCES[name]
    target = into / name
    if not (target / ".git").exists():
        target.mkdir(parents=True, exist_ok=True)
        for command in (["init", "-q"], ["remote", "add", "origin", url],
                        ["fetch", "-q", "--depth", "1", "origin", commit], ["checkout", "-q", "FETCH_HEAD"]):
            subprocess.run(["git", "-C", str(target), *command], check=True)
    return target


def write_truth(folder: Path, rows: list[dict]) -> None:
    with open(folder / "truth.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def invoice2data_truth(folder: Path) -> list[dict]:
    rows = []
    for pdf in sorted(folder.glob("*.pdf")):
        answers = json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
        answers = answers[0] if isinstance(answers, list) else answers
        if answers.get("amount") is None:
            continue
        rows.append({"file": pdf.name, "id": answers["invoice_number"], "vendor": answers["issuer"],
                     "date": str(answers["date"])[:10], "amount": answers["amount"]})
    return rows


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first(root, name: str, under: str | None = None):
    scope = [e for e in root.iter() if _local(e.tag) == under] if under else [root]
    for parent in scope:
        for element in parent.iter():
            if _local(element.tag) == name and element is not parent:
                return (element.text or "").strip() or None
    return None


def embedded_truth(xml: bytes) -> dict | None:
    """The invoice's own data (UN/CEFACT Cross Industry Invoice, as ZUGFeRD and Factur-X embed it)."""
    if len(xml) > 2_000_000 or b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        return None            # never expand entities from a downloaded file
    root = ET.fromstring(xml)
    document = next((e for e in root.iter() if _local(e.tag) in ("ExchangedDocument", "HeaderExchangedDocument")), None)
    if document is None:
        return None
    number = next(((e.text or "").strip() for e in document if _local(e.tag) == "ID"), None)
    code = next(((e.text or "").strip() for e in document if _local(e.tag) == "TypeCode"), None)
    issued = _first(document, "DateTimeString", under="IssueDateTime") or ""
    seller = _first(root, "Name", under="SellerTradeParty")
    total, due = _first(root, "GrandTotalAmount"), _first(root, "DuePayableAmount")
    if not (number and seller and total and re.fullmatch(r"\d{8}", issued)):
        return None
    return {"id": number, "vendor": seller.splitlines()[-1].strip(), "amount": float(total),
            "date": f"{issued[:4]}-{issued[4:6]}-{issued[6:]}", "amount_due": float(due) if due else "",
            # a credit note by its type, or any document whose grand total is negative
            "kind": "credit" if code == "381" or float(total) < 0 else ("invoice" if code in ("380", "384", "389") else "")}


def corpus_truth(folder: Path) -> list[dict]:
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("This script needs pypdf to read the XML embedded in e-invoices: pip install pypdf")
    rows = []
    for pdf in sorted(folder.rglob("*.pdf")):
        try:
            attachments = PdfReader(str(pdf)).attachments
            xml = next((files[0] for name, files in attachments.items() if name.lower().endswith(".xml")), None)
            answers = embedded_truth(xml) if xml else None
        except Exception:      # a deliberately broken sample: there are several in the corpus
            answers = None
        if answers:
            rows.append({"file": pdf.relative_to(folder).as_posix(), **answers})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep", help="download into this folder and keep it (default: a temporary folder)")
    parser.add_argument("--min-all-four", type=float, default=0.0,
                        help="fail unless at least this share of documents has all four fields right, in each set")
    parser.add_argument("--min-exact", type=float, default=0.0,
                        help="fail unless at least this share of e-invoices is read exactly from their own data")
    parser.add_argument("--max-totals-warnings", type=int, default=None,
                        help="fail if more documents than this are told their totals don't add up")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(args.keep or temporary)
        first = fetch("invoice2data", work) / "tests" / "compare"
        write_truth(first, invoice2data_truth(first))
        second = fetch("corpus", work)
        rows = corpus_truth(second)
        write_truth(second, rows)
        vendors = sorted({row["vendor"] for row in rows})
        # The page is what is scored: the answers come from the e-invoice data, so reading
        # that data would score itself. It is measured separately below.
        reports = {
            "Real company invoices (invoice2data)": score_folder(first, embedded=False),
            "E-invoices, page only, vendors used for tuning": score_folder(second, groups=set(vendors[0::2]),
                                                                           embedded=False),
            "E-invoices, page only, vendors held out": score_folder(second, groups=set(vendors[1::2]),
                                                                    embedded=False),
        }
        exact = score_folder(second)
    failed = False
    for title, report in reports.items():
        print(format_report(report, title), end="\n\n")
        failed |= report["all_four"] is None or report["all_four"] < args.min_all_four
    warnings = sum(r["totals_warnings"] for r in reports.values())
    documents = sum(r["documents"] for r in reports.values())
    right = sum(round(r["all_four"] * r["documents"]) for r in reports.values())
    print(f"Overall, reading the page: all four fields right on {right} of {documents} documents "
          f"({right / documents:.0%}); {warnings} wrongly told the totals don't add up.")
    print()
    print(format_report(exact, "E-invoices read from their own data (ZUGFeRD / Factur-X)"))
    print("  (Told \"the totals don't add up\" above includes PDFs whose page contradicts their own data:"
          " several samples in this corpus really do.)")
    if args.max_totals_warnings is not None and warnings > args.max_totals_warnings:
        failed = True
    if exact["all_four"] is None or exact["all_four"] < args.min_exact:
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
