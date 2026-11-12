"""Score invoice reading on real files: a folder of invoices plus the right answers.

    python -m invoice_detector.cli evaluate-documents --folder DIR

DIR holds the invoices (PDF, Excel, images; sub-folders are fine) and truth.csv:

    file,id,vendor,date,amount,amount_due,kind
    acme/inv-1042.pdf,1042,Acme Supplies Ltd,2026-03-05,1250.00,,invoice

id, vendor, date (YYYY-MM-DD) and amount are what a careful person would type in.
amount_due (optional) is what was still to pay if that differs from the invoice total:
either counts as right. kind (optional) is "invoice" or "credit".

Generated layouts (doc_samples.py) can only show that the reader handles what its author
thought of. This measures it on documents nobody here designed, and it is the number to
quote. Use it on a sample of your own vendors' invoices before relying on the reader.
"""
from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from pathlib import Path

from .importer import FileProblem

FIELDS = ("id", "vendor", "date", "amount")
SUFFIXES = {".pdf", ".xlsx", ".xlsm", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}


def _squash(text) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def vendor_matches(read, truth) -> bool:
    """The same company, allowing for what surrounds a name on paper: "Acme" for
    "Acme Supplies Ltd", or the legal name where the truth has the trading name."""
    got = _squash(read or "")
    wanted = [_squash(line) for line in str(truth).splitlines() if re.search(r"[A-Za-z]{3}", line)]
    return bool(got) and any(w and (w in got or (len(got) >= 4 and got in w)) for w in wanted)


def field_right(name: str, read, row: dict) -> bool:
    truth = row.get(name)
    if read is None or truth in (None, ""):
        return False
    if name == "amount":
        targets = [abs(float(v)) for v in (truth, row.get("amount_due")) if v not in (None, "")]
        return any(abs(float(read) - t) < 0.005 for t in targets)
    if name == "vendor":
        return vendor_matches(read, truth)
    if name == "id":
        return str(read).lstrip("#").strip() == str(truth).lstrip("#").strip()
    return str(read) == str(truth)[:10]


def load_truth(path: Path) -> dict[str, dict]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"file", *FIELDS} <= set(rows[0]):
        raise FileProblem(f"{path.name} needs the columns: file, id, vendor, date, amount.")
    return {row["file"]: row for row in rows}


def score_folder(folder, truth_path=None, company: str = "", isolate: bool = True, groups=None) -> dict:
    """Read every file listed in truth.csv and compare. Identical files are counted once.
    groups: only rows whose vendor is in this set (used to keep a hold-out apart)."""
    folder = Path(folder)
    truth = load_truth(Path(truth_path) if truth_path else folder / "truth.csv")
    if isolate:
        from .isolation import run_isolated

        def read(name, data):
            return run_isolated("invoice_detector.documents:read_document", name, data, company)
    else:
        from .documents import read_document

        def read(name, data):
            return read_document(name, data, company)

    right = dict.fromkeys(FIELDS, 0)
    by_vendor: dict[str, list[int]] = {}
    seen: set[str] = set()
    documents = all_right = unreadable = totals_warnings = kinds = kinds_right = 0
    mistakes = []
    for rel, row in sorted(truth.items()):
        if groups is not None and row["vendor"] not in groups:
            continue
        path = (folder / rel).resolve()
        if folder.resolve() not in path.parents or path.suffix.lower() not in SUFFIXES or not path.is_file():
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        documents += 1
        try:
            ex = read(path.name, data)
        except FileProblem as exc:
            unreadable += 1
            mistakes.append({"file": rel, "field": "file", "expected": "readable", "read": str(exc)})
            by_vendor.setdefault(row["vendor"], [0, 0])[1] += 1
            continue
        ok = {f: field_right(f, ex.fields.get(f), row) for f in FIELDS}
        for f in FIELDS:
            right[f] += ok[f]
            if not ok[f]:
                mistakes.append({"file": rel, "field": f, "expected": row[f], "read": ex.fields.get(f)})
        tally = by_vendor.setdefault(row["vendor"], [0, 0])
        tally[0] += all(ok.values())
        tally[1] += 1
        all_right += all(ok.values())
        totals_warnings += any("add up" in w for w in ex.warnings)
        if row.get("kind") in ("invoice", "credit"):
            kinds += 1
            kinds_right += ex.kind == row["kind"]
    return {"documents": documents, "vendors": len(by_vendor), "unreadable": unreadable,
            "accuracy": {f: round(right[f] / documents, 4) if documents else None for f in FIELDS},
            "all_four": round(all_right / documents, 4) if documents else None,
            "all_four_per_vendor": round(sum(a / n for a, n in by_vendor.values()) / len(by_vendor), 4)
            if by_vendor else None,
            "totals_warnings": totals_warnings,
            "kind": round(kinds_right / kinds, 4) if kinds else None,
            "mistakes": mistakes}


def format_report(report: dict, title: str = "") -> str:
    if not report["documents"]:
        return "No files listed in truth.csv were found."
    lines = [f"{title or 'Read'}: {report['documents']} documents from {report['vendors']} vendors"]
    for f in FIELDS:
        lines.append(f"  {f:8s} {report['accuracy'][f]:.0%}")
    lines.append(f"  all four right: {report['all_four']:.0%} of documents "
                 f"({report['all_four_per_vendor']:.0%} averaged per vendor)")
    lines.append(f"  told \"the totals don't add up\": {report['totals_warnings']}"
                 + (f"   invoice / credit note told apart: {report['kind']:.0%}" if report["kind"] is not None else ""))
    if report["unreadable"]:
        lines.append(f"  could not be read at all: {report['unreadable']}")
    return "\n".join(lines)
