"""Exact reading of e-invoices: the structured data an invoice carries alongside the page.

A growing share of European invoices are e-invoices. The data comes in one of two shapes:

- **UN/CEFACT Cross Industry Invoice (CII)**: ZUGFeRD 1 and 2, Factur-X and XRechnung (CII
  syntax). ZUGFeRD and Factur-X put the XML *inside* an ordinary-looking PDF.
- **OASIS UBL 2.1** (``Invoice`` / ``CreditNote``): PEPPOL and XRechnung (UBL syntax),
  usually sent as a bare .xml file.

When the data is there it is the invoice, exactly: no reading of the layout is needed. It is
also a check. A PDF whose visible page and embedded data disagree has been altered, or was
made by a system that is broken; documents.read_document compares them and says so.

Safety. The XML comes from outside, so it is parsed with defusedxml (no DTDs, no entities,
so no "billion laughs" and no reading files off the server), at most 2 MB of it, and only
from a known attachment in a PDF that has already passed the PDF reader's own limits.
"""
from __future__ import annotations

import io
import re
from datetime import date

from .importer import FileProblem

MAX_XML = 2 * 1024 * 1024
MAX_ATTACHMENTS = 20

# Attachment names the standards use, in order of preference.
PREFERRED_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "zugferd_invoice.xml", "xrechnung.xml", "order-x.xml")

# UNTDID 1001 document types that are credit notes. 384 ("corrected invoice") is a credit
# only when its total is negative, which the sign check below catches.
CREDIT_TYPES = {"81", "83", "261", "262", "296", "308", "381", "396", "420", "458", "532"}


class Data(dict):
    """The fields found, plus .format (a human name for the standard) and .totals."""
    format = ""
    totals: dict


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(el, name):
    return [c for c in el if _local(c.tag) == name]


def _path(el, *names):
    """The first element down a path of local names (namespace-agnostic). None if missing."""
    current = [el]
    for name in names:
        current = [c for e in current for c in _children(e, name)]
        if not current:
            return None
    return current[0]


def _text(el, *names) -> str | None:
    found = _path(el, *names) if names else el
    if found is None or found.text is None:
        return None
    text = re.sub(r"\s+", " ", found.text).strip()
    return text or None


_IDENTIFIER_LINE = re.compile(r"(?i)^\s*(gln|lief(erant)?[- .]*nr|kunden[- .]*nr|kd[- .]*nr|ust|vat|tax|siret|siren|"
                              r"duns|id)\b[^A-Za-z]*\S*\s*$|^\s*[\d\s:/.-]+$")


def _name(el) -> str | None:
    """A party's name. Some systems put identifier lines into it ("GLN 4333741000005" /
    "Lief-Nr: 549910" / "MUSTERLIEFERANT GMBH"): keep the lines that are the name."""
    if el is None or not el.text:
        return None
    lines = [line.strip() for line in el.text.splitlines() if line.strip()]
    kept = [line for line in lines if not _IDENTIFIER_LINE.match(line)] or lines
    return re.sub(r"\s+", " ", " ".join(kept)).strip() or None


def _descendant(el, name):
    return next((d for d in el.iter() if _local(d.tag) == name), None)


def _amount(el, currency: str | None = None) -> float | None:
    """An amount element's value. CII may repeat an amount in the tax currency: take the one
    in the invoice currency, or the first."""
    candidates = [el] if not isinstance(el, list) else el
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return None
    if currency:
        same = [c for c in candidates if (c.get("currencyID") or currency) == currency]
        candidates = same or candidates
    try:
        return round(float((candidates[0].text or "").strip()), 2)
    except ValueError:
        return None


def _cii_date(el) -> str | None:
    """<udt:DateTimeString format="102">20180605</udt:DateTimeString> -> 2018-06-05."""
    raw = _text(el, "DateTimeString") if el is not None else None
    if raw and re.fullmatch(r"\d{8}", raw):
        try:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:])).isoformat()
        except ValueError:
            return None
    return None


def _iso_date(raw: str | None) -> str | None:
    if raw and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw[:10]):
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            return None
    return None


def parse_xml(data: bytes) -> Data | None:
    """Fields from CII or UBL XML. None if the XML isn't an invoice in either standard."""
    if len(data) > MAX_XML:
        raise FileProblem("The e-invoice data is larger than 2 MB.")
    try:
        from defusedxml import ElementTree as SafeET
        from defusedxml.common import DefusedXmlException
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise FileProblem("Reading e-invoices needs defusedxml on the server.") from exc
    try:
        root = SafeET.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException as exc:
        raise FileProblem("This XML file uses features that aren't allowed in an invoice (a DTD or entities).") from exc
    except Exception as exc:
        raise FileProblem("This XML file is damaged (it isn't complete, well-formed XML).") from exc
    name = _local(root.tag)
    if name in ("CrossIndustryInvoice", "CrossIndustryDocument"):
        return _parse_cii(root)
    if name in ("Invoice", "CreditNote"):
        return _parse_ubl(root)
    if name == "FatturaElettronica":
        return _parse_fattura(root)
    return None


def _finish(out: Data, type_code: str | None, total: float | None) -> Data:
    out["kind"] = "credit" if (type_code in CREDIT_TYPES or (total is not None and total < 0)) else "invoice"
    for key in ("amount", "amount_due"):
        if out.get(key) is not None:
            out[key] = abs(out[key])
    out.totals = {k: abs(v) for k, v in out.totals.items() if v is not None}
    return out


def _parse_cii(root) -> Data:
    out = Data()
    v2 = _local(root.tag) == "CrossIndustryInvoice"
    doc = _path(root, "ExchangedDocument" if v2 else "HeaderExchangedDocument")
    trade = _path(root, "SupplyChainTradeTransaction" if v2 else "SpecifiedSupplyChainTradeTransaction")
    if doc is None or trade is None:
        return out
    agreement = _path(trade, "ApplicableHeaderTradeAgreement" if v2 else "ApplicableSupplyChainTradeAgreement")
    settlement = _path(trade, "ApplicableHeaderTradeSettlement" if v2 else "ApplicableSupplyChainTradeSettlement")
    out.format = "ZUGFeRD / Factur-X" if v2 else "ZUGFeRD 1"
    out["id"] = _text(doc, "ID")
    type_code = _text(doc, "TypeCode")
    out["date"] = _cii_date(_path(doc, "IssueDateTime"))
    if agreement is not None:
        out["vendor"] = _name(_path(agreement, "SellerTradeParty", "Name"))
        out["customer"] = _name(_path(agreement, "BuyerTradeParty", "Name"))
    total = None
    out.totals = {}
    if settlement is not None:
        currency = _text(settlement, "InvoiceCurrencyCode")
        out["currency"] = currency
        summary = _path(settlement, "SpecifiedTradeSettlementHeaderMonetarySummation" if v2
                        else "SpecifiedTradeSettlementMonetarySummation")
        if summary is not None:
            total = _amount(_children(summary, "GrandTotalAmount"), currency)
            out["amount"] = total
            out["amount_due"] = _amount(_children(summary, "DuePayableAmount"), currency)
            out.totals = {"subtotal": _amount(_children(summary, "TaxBasisTotalAmount"), currency),
                          "tax": _amount(_children(summary, "TaxTotalAmount"), currency),
                          "lines": _amount(_children(summary, "LineTotalAmount"), currency)}
        for means in _children(settlement, "SpecifiedTradeSettlementPaymentMeans"):
            iban = _text(means, "PayeePartyCreditorFinancialAccount", "IBANID") or \
                _text(means, "PayeePartyCreditorFinancialAccount", "ProprietaryID")
            if iban:
                out["bank_account"] = re.sub(r"\s+", "", iban)
                break
        reference = _path(settlement, "InvoiceReferencedDocument")
        if reference is not None:
            out["reference"] = _text(reference, "IssuerAssignedID")
    if out.get("reference") is None and agreement is not None:
        out["reference"] = _text(agreement, "InvoiceReferencedDocument", "IssuerAssignedID")
    return _finish(out, type_code, total)


def _parse_ubl(root) -> Data:
    out = Data()
    credit_note = _local(root.tag) == "CreditNote"
    out.format = "UBL credit note" if credit_note else "UBL (PEPPOL / XRechnung)"
    out["id"] = _text(root, "ID")
    type_code = _text(root, "CreditNoteTypeCode" if credit_note else "InvoiceTypeCode")
    if credit_note and not type_code:
        type_code = "381"
    out["date"] = _iso_date(_text(root, "IssueDate"))
    currency = _text(root, "DocumentCurrencyCode")
    out["currency"] = currency

    def party(role):
        p = _path(root, role, "Party")
        if p is None:
            return None
        return (_text(p, "PartyLegalEntity", "RegistrationName") or _text(p, "PartyName", "Name"))
    out["vendor"] = party("AccountingSupplierParty")
    out["customer"] = party("AccountingCustomerParty")
    money = _path(root, "LegalMonetaryTotal")
    total = None
    out.totals = {}
    if money is not None:
        total = _amount(_children(money, "TaxInclusiveAmount"), currency)
        out["amount"] = total
        out["amount_due"] = _amount(_children(money, "PayableAmount"), currency)
        out.totals = {"subtotal": _amount(_children(money, "TaxExclusiveAmount"), currency),
                      "lines": _amount(_children(money, "LineExtensionAmount"), currency)}
    taxes = [t for t in _children(root, "TaxTotal")]
    tax = [_amount(_children(t, "TaxAmount"), currency) for t in taxes]
    tax = [t for t in tax if t is not None]
    if tax:
        out.totals["tax"] = tax[0]
    for means in _children(root, "PaymentMeans"):
        account = _text(means, "PayeeFinancialAccount", "ID")
        if account:
            out["bank_account"] = re.sub(r"\s+", "", account)
            break
    out["reference"] = _text(root, "BillingReference", "InvoiceDocumentReference", "ID")
    return _finish(out, type_code, total)


# FatturaPA document types that are credit notes (nota di credito, incl. self-billed ones).
FATTURA_CREDIT = {"TD04", "TD08"}


def _number(text: str | None) -> float | None:
    try:
        return round(float(text), 2) if text not in (None, "") else None
    except ValueError:
        return None


def _parse_fattura(root) -> Data:
    """Italy's FatturaPA (SDI), required for all Italian B2B invoices since 2019."""
    out = Data()
    out.format = "FatturaPA"
    out.totals = {}
    header, bodies = _path(root, "FatturaElettronicaHeader"), _children(root, "FatturaElettronicaBody")
    if header is None or not bodies:
        return out

    def who(party):
        data = _path(header, party, "DatiAnagrafici", "Anagrafica")
        if data is None:
            return None
        return _text(data, "Denominazione") or " ".join(
            x for x in (_text(data, "Nome"), _text(data, "Cognome")) if x) or None
    out["vendor"], out["customer"] = who("CedentePrestatore"), who("CessionarioCommittente")
    body = bodies[0]
    general = _path(body, "DatiGenerali", "DatiGeneraliDocumento")
    if general is None:
        return out
    type_code = _text(general, "TipoDocumento")
    out["id"] = _text(general, "Numero")
    out["date"] = _iso_date(_text(general, "Data"))
    out["currency"] = _text(general, "Divisa")
    goods = _path(body, "DatiBeniServizi")
    summaries = _children(goods if goods is not None else body, "DatiRiepilogo")
    base = [_number(_text(r, "ImponibileImporto")) for r in summaries]
    tax = [_number(_text(r, "Imposta")) for r in summaries]
    if summaries and None not in base and None not in tax:
        out.totals = {"subtotal": round(sum(base), 2), "tax": round(sum(tax), 2)}
    total = _number(_text(general, "ImportoTotaleDocumento"))
    if total is None and out.totals:
        total = round(out.totals["subtotal"] + out.totals["tax"], 2)
    out["amount"] = total
    payment = _path(body, "DatiPagamento", "DettaglioPagamento")
    if payment is not None:
        if _text(payment, "IBAN"):
            out["bank_account"] = re.sub(r"\s+", "", _text(payment, "IBAN"))
        out["amount_due"] = _number(_text(payment, "ImportoPagamento"))
    out["reference"] = _text(body, "DatiGenerali", "DatiFattureCollegate", "IdDocumento")
    if len(bodies) > 1:
        out["batch"] = len(bodies)
    return _finish(out, "381" if type_code in FATTURA_CREDIT else type_code, total)


def from_pdf(data: bytes) -> Data | None:
    """The e-invoice data embedded in a PDF (ZUGFeRD / Factur-X / XRechnung), or None."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - declared dependency
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        names = list(reader.attachments.keys())[:MAX_ATTACHMENTS]
    except Exception:
        return None
    xml_names = [n for n in names if str(n).lower().endswith(".xml")]
    xml_names.sort(key=lambda n: (PREFERRED_NAMES.index(n.lower()) if n.lower() in PREFERRED_NAMES else 99, n))
    for name in xml_names:
        try:
            content = reader.attachments[name][0]
        except Exception:
            continue
        if len(content) > MAX_XML:
            continue
        try:
            parsed = parse_xml(content)
        except FileProblem:
            continue           # a hostile attachment: read the page instead
        if parsed and parsed.get("id") and parsed.get("amount") is not None:
            return parsed
    return None


def looks_like_xml(data: bytes) -> bool:
    head = data[:200].lstrip(b"\xef\xbb\xbf \t\r\n")
    return head.startswith(b"<?xml") or head.startswith(b"<")
