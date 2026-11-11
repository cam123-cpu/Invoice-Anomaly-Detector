"""Turning uploaded invoice documents into stored invoices, in two steps.

1. extract(): read one file and return a *draft*: the fields found, how sure each one is,
   and anything worth a warning. Nothing is saved. The draft carries a signed token
   recording what the machine read, so the server needs no memory between the steps.

2. import_drafts(): the person has checked (and maybe corrected) the drafts; save them.
   The token proves which file each draft came from and what was originally read, so the
   record keeps both: the final values, and which fields a person changed.

        upload ─> isolate & read ─> (optional AI) ─> match vendor ─> draft + token
                                                                        │ person checks
        audit  <─ detect <─ save invoice + document record (one transaction) <─┘

Like accounts.py this has no web framework in it, so its rules are tested directly.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date

from .ai_extract import AIError, extract_with_claude, merge
from .db import Database
from .documents import (LABEL_NAMES, REQUIRED, Extraction, document_text, kind_of, norm, read_document, similar,
                        totals_issue)
from .importer import ID_PATTERN, FileProblem
from .isolation import run_isolated
from .security import AuthError, RateLimiter, RateLimitError, TokenSigner

DRAFT_TTL = 4 * 3600          # a person has four hours to check a batch
MAX_BATCH = 200
AI_PER_HOUR = 100             # per company


class IntakeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class DocumentIntake:
    def __init__(self, db: Database, signer: TokenSigner, settings, ai_post=None, isolate: bool = True):
        self.db, self.signer, self.settings = db, signer, settings
        self.ai_post = ai_post
        self.isolate = isolate
        self.ai_limiter = RateLimiter(max_attempts=AI_PER_HOUR, window=3600, lockout=3600)

    # ------------------------------------------------------------ step 1
    def ai_available(self, org: dict) -> bool:
        return bool(self.settings.anthropic_api_key) and bool(org.get("ai_extraction"))

    def _read_locally(self, filename: str, data: bytes, company: str) -> Extraction:
        if self.isolate:
            return run_isolated("invoice_detector.documents:read_document", filename, data, company)
        return read_document(filename, data, company)

    def extract(self, user: dict, filename: str, data: bytes) -> dict:
        org = self.db.get_org(user["org"])
        sha = hashlib.sha256(data).hexdigest()
        previous = self.db.document_by_hash(user["org"], sha)
        if previous:
            return {"file": filename, "status": "already_imported", "invoice_id": previous["invoice_id"],
                    "message": f"This file was already imported as invoice {previous['invoice_id']} "
                               f"by {previous['uploaded_by']}."}
        problem, local = None, None
        try:
            local = self._read_locally(filename, data, org["name"])
        except FileProblem as exc:
            problem = str(exc)

        ex = local
        unsure = local is None or local.missing or any(local.confidence.get(f, 0) < 0.6 for f in REQUIRED)
        if unsure and self.ai_available(org):
            try:
                self.ai_limiter.check(user["org"])
                self.ai_limiter.record_failure(user["org"])   # counts every use
                kind = kind_of(filename, data)
                text = None
                if kind == "xlsx":
                    text = (run_isolated("invoice_detector.documents:document_text", filename, data)
                            if self.isolate else document_text(filename, data))
                kwargs = {"post": self.ai_post} if self.ai_post else {}
                ai = extract_with_claude(kind, data, text, org["name"], self.settings.anthropic_api_key, **kwargs)
                ex = merge(local, ai)
                self.db.audit(user["org"], user["sub"], "ai_document_read",
                              f"{filename[:80]} sent to Anthropic for reading")
            except RateLimitError:
                if ex is not None:
                    ex.warnings.append("AI reading is paused: this company used its hourly limit.")
            except (AIError, FileProblem) as exc:
                if ex is not None:
                    ex.warnings.append(f"AI reading didn't help: {exc}")
                elif problem is None:
                    problem = str(exc)
        if ex is None:
            return {"file": filename, "status": "unreadable", "message": problem or "This file could not be read."}

        self._match_vendor(user["org"], ex)
        number = ex.fields.get("id")
        if number and self.db.get_invoices(user["org"], [str(number)]):
            ex.warnings.insert(0, f"Invoice number {number} is already on file. It will be skipped unless you change it.")
        draft = self.signer.issue({"org": user["org"], "sub": user["sub"], "sha": sha, "file": filename[:200],
                                   "method": ex.method, "read": ex.fields, "totals": ex.totals},
                                  "doc", DRAFT_TTL)
        return {"file": filename, "status": "ready" if not ex.missing else "incomplete", "draft": draft,
                "fields": ex.fields, "confidence": ex.confidence, "source": ex.source, "totals": ex.totals,
                "warnings": ex.warnings, "method": ex.method, "currency": ex.currency, "customer": ex.customer,
                "missing": ex.missing, "ai_available": self.ai_available(org)}

    def _match_vendor(self, org_id: str, ex: Extraction) -> None:
        """Use the company's existing spelling of a vendor when the only differences are
        formatting (case, punctuation, "Inc" vs "INC."), so one vendor doesn't become two.

        A near-miss in the *letters* ("Acme Suplies" vs "Acme Supplies") is never merged: that
        is exactly what a look-alike vendor scam looks like, and the detector must see it."""
        name = ex.fields.get("vendor")
        if not name:
            return
        known = self.db.vendor_names(org_id)
        exact = [k for k in known if _canonical(k) == _canonical(name)]
        if exact:
            ex.fields["vendor"] = exact[0]
            return
        near = sorted(((similar(name, k), k) for k in known), reverse=True)
        if near and near[0][0] >= 0.85:
            ex.warnings.append(f"\"{name}\" is spelled almost like your vendor \"{near[0][1]}\". "
                               "A typo, or a look-alike? Keep it as printed unless you're sure.")

    # ------------------------------------------------------------ step 2
    @staticmethod
    def _clean(item: dict) -> tuple[dict | None, str | None]:
        """Validate one checked draft. (row, None) or (None, reason)."""
        inv_id = str(item.get("id") or "").strip()
        vendor = re.sub(r"\s+", " ", str(item.get("vendor") or "")).strip()
        bank = str(item.get("bank_account") or "").strip() or None
        if not ID_PATTERN.match(inv_id):
            return None, "the invoice number is missing or has unusual characters"
        if not 1 <= len(vendor) <= 120:
            return None, "the vendor is missing"
        try:
            amount = round(float(item.get("amount")), 2)
        except (TypeError, ValueError):
            return None, "the amount is missing"
        if not 0 < amount < 1e12:
            return None, "the amount must be greater than zero"
        try:
            when = date.fromisoformat(str(item.get("date") or ""))
        except ValueError:
            return None, "the date is missing"
        if not 1990 <= when.year <= 2100:
            return None, "the date is outside 1990-2100"
        if bank and len(bank) > 64:
            return None, "the bank account is too long"
        return {"id": inv_id, "vendor": vendor, "amount": amount, "date": when.isoformat(),
                "bank_account": bank}, None

    def import_drafts(self, user: dict, items: list[dict]) -> dict:
        if len(items) > MAX_BATCH:
            raise IntakeError(413, f"At most {MAX_BATCH} documents at a time.")
        rows, skipped, seen_files, seen_ids = [], [], set(), set()
        for item in items:
            try:
                draft = self.signer.verify(str(item.get("draft") or ""), "doc")
            except AuthError:
                skipped.append({"file": item.get("file"), "reason": "the check expired; read the file again"})
                continue
            if draft.get("org") != user["org"]:
                raise IntakeError(403, "That draft belongs to another company.")
            row, reason = self._clean(item)
            if row is None:
                skipped.append({"file": draft["file"], "reason": reason})
                continue
            if draft["sha"] in seen_files or self.db.document_by_hash(user["org"], draft["sha"]):
                skipped.append({"file": draft["file"], "reason": "this file was already imported"})
                continue
            if row["id"] in seen_ids:
                skipped.append({"file": draft["file"], "reason": f"invoice {row['id']} appears twice in this batch"})
                continue
            seen_files.add(draft["sha"])
            seen_ids.add(row["id"])
            read = draft.get("read") or {}
            corrections = {f: [read.get(f), row[f]] for f in ("id", "vendor", "amount", "date", "bank_account")
                           if not _same(read.get(f), row[f])}
            row.update(sha256=draft["sha"], filename=draft["file"], method=draft["method"],
                       corrections=corrections, totals_issue=totals_issue(row["amount"], draft.get("totals") or {}))
            rows.append(row)
        already = set(self.db.add_document_invoices(user["org"], rows, user["sub"])) if rows else set()
        for r in rows:
            if r["id"] in already:
                skipped.append({"file": r["filename"], "reason": f"invoice {r['id']} is already on file"})
        added = [r for r in rows if r["id"] not in already]
        corrected = sum(len(r["corrections"]) for r in added)
        if added:
            self.db.audit(user["org"], user["sub"], "documents_imported",
                          f"{len(added)} invoice{'s' if len(added) != 1 else ''} from documents"
                          + (f", {corrected} field{'s' if corrected != 1 else ''} corrected by hand" if corrected else ""))
        return {"added": len(added), "ids": [r["id"] for r in added], "skipped": skipped,
                "fields_corrected": corrected,
                "totals_issues": sum(1 for r in added if r["totals_issue"])}


def _canonical(name: str) -> str:
    """A vendor name without formatting: lower case, no punctuation, no legal suffix."""
    words = re.sub(r"[^a-z0-9 ]+", " ", norm(name)).split()
    while words and words[-1] in ("inc", "llc", "ltd", "limited", "corp", "corporation", "co", "plc", "gmbh"):
        words.pop()
    return " ".join(words)


def _same(read, final) -> bool:
    if read is None or final is None:
        return read == final or (not read and not final)
    if isinstance(read, (int, float)) or isinstance(final, (int, float)):
        try:
            return abs(float(read) - float(final)) < 0.005
        except (TypeError, ValueError):
            return False
    return str(read).strip() == str(final).strip()


__all__ = ["DocumentIntake", "IntakeError", "LABEL_NAMES"]
