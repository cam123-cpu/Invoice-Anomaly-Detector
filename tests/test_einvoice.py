"""E-invoices: exact reading of CII (ZUGFeRD / Factur-X / XRechnung), UBL (PEPPOL) and
FatturaPA, the comparison with the visible page, and hostile XML."""
import io
import unittest

from invoice_detector import doc_samples, einvoice
from invoice_detector.documents import kind_of, read_document, totals_issue
from invoice_detector.importer import FileProblem

CII = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
    xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
    xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
  <rsm:ExchangedDocument>
    <ram:ID>{id}</ram:ID>
    <ram:TypeCode>{type}</ram:TypeCode>
    <ram:IssueDateTime><udt:DateTimeString format="102">20260305</udt:DateTimeString></ram:IssueDateTime>
  </rsm:ExchangedDocument>
  <rsm:SupplyChainTradeTransaction>
    <ram:ApplicableHeaderTradeAgreement>
      <ram:SellerTradeParty><ram:Name>GLN 4000001000005
Lieferant GmbH</ram:Name></ram:SellerTradeParty>
      <ram:BuyerTradeParty><ram:Name>Kunden AG</ram:Name></ram:BuyerTradeParty>
    </ram:ApplicableHeaderTradeAgreement>
    <ram:ApplicableHeaderTradeSettlement>
      <ram:InvoiceCurrencyCode>EUR</ram:InvoiceCurrencyCode>
      <ram:SpecifiedTradeSettlementPaymentMeans>
        <ram:PayeePartyCreditorFinancialAccount><ram:IBANID>DE89 3704 0044 0532 0130 00</ram:IBANID></ram:PayeePartyCreditorFinancialAccount>
      </ram:SpecifiedTradeSettlementPaymentMeans>
      <ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        <ram:LineTotalAmount>{net}</ram:LineTotalAmount>
        <ram:TaxBasisTotalAmount>{net}</ram:TaxBasisTotalAmount>
        <ram:TaxTotalAmount currencyID="EUR">{tax}</ram:TaxTotalAmount>
        <ram:GrandTotalAmount>{total}</ram:GrandTotalAmount>
        <ram:DuePayableAmount>{total}</ram:DuePayableAmount>
      </ram:SpecifiedTradeSettlementHeaderMonetarySummation>
      {ref}
    </ram:ApplicableHeaderTradeSettlement>
  </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>"""

UBL = """<?xml version="1.0" encoding="UTF-8"?>
<{root} xmlns="urn:oasis:names:specification:ubl:schema:xsd:{root}-2"
  xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
  xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>UBL-77</cbc:ID>
  <cbc:IssueDate>2026-02-14</cbc:IssueDate>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
  <cac:BillingReference><cac:InvoiceDocumentReference><cbc:ID>UBL-70</cbc:ID></cac:InvoiceDocumentReference></cac:BillingReference>
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>Trading Name</cbc:Name></cac:PartyName>
    <cac:PartyLegalEntity><cbc:RegistrationName>Leverancier B.V.</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:PaymentMeans><cac:PayeeFinancialAccount><cbc:ID>NL91ABNA0417164300</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>
  <cac:TaxTotal><cbc:TaxAmount currencyID="EUR">21.00</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">100.00</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">100.00</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">121.00</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">121.00</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
</{root}>"""

FATTURA = """<?xml version="1.0" encoding="UTF-8"?>
<p:FatturaElettronica versione="FPR12" xmlns:p="http://ivaservizi.agenziaentrate.gov.it/docs/xsd/fatture/v1.2">
  <FatturaElettronicaHeader>
    <CedentePrestatore><DatiAnagrafici><Anagrafica><Denominazione>Officine Rossi S.r.l.</Denominazione></Anagrafica></DatiAnagrafici></CedentePrestatore>
    <CessionarioCommittente><DatiAnagrafici><Anagrafica><Nome>Mario</Nome><Cognome>Bianchi</Cognome></Anagrafica></DatiAnagrafici></CessionarioCommittente>
  </FatturaElettronicaHeader>
  {bodies}
</p:FatturaElettronica>"""
FATTURA_BODY = """<FatturaElettronicaBody>
    <DatiGenerali>
      <DatiGeneraliDocumento><TipoDocumento>{type}</TipoDocumento><Divisa>EUR</Divisa><Data>2026-01-18</Data><Numero>{n}</Numero></DatiGeneraliDocumento>
      <DatiFattureCollegate><IdDocumento>2025/990</IdDocumento></DatiFattureCollegate>
    </DatiGenerali>
    <DatiBeniServizi>
      <DatiRiepilogo><ImponibileImporto>100.00</ImponibileImporto><Imposta>22.00</Imposta></DatiRiepilogo>
      <DatiRiepilogo><ImponibileImporto>50.00</ImponibileImporto><Imposta>5.00</Imposta></DatiRiepilogo>
    </DatiBeniServizi>
    <DatiPagamento><DettaglioPagamento><IBAN>IT60X0542811101000000123456</IBAN><ImportoPagamento>177.00</ImportoPagamento></DettaglioPagamento></DatiPagamento>
  </FatturaElettronicaBody>"""


def cii(id="RE-2026-118", type="380", net="100.00", tax="19.00", total="119.00", ref=""):
    if ref:
        ref = f"<ram:InvoiceReferencedDocument><ram:IssuerAssignedID>{ref}</ram:IssuerAssignedID></ram:InvoiceReferencedDocument>"
    return CII.format(id=id, type=type, net=net, tax=tax, total=total, ref=ref).encode()


def pdf_with(xml: bytes, name: str = "factur-x.xml") -> tuple[bytes, dict]:
    """One of the sample PDF invoices, with e-invoice data attached the way Factur-X does."""
    from pypdf import PdfReader, PdfWriter
    _, data, doc = next(s for s in doc_samples.sample_docs() if s[0].endswith(".pdf"))
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.add_attachment(name, xml)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), doc.truth()


class ExactReadingTests(unittest.TestCase):
    def test_cii(self):
        d = einvoice.parse_xml(cii())
        self.assertEqual((d["id"], d["date"], d["vendor"], d["amount"], d["kind"], d["currency"]),
                         ("RE-2026-118", "2026-03-05", "Lieferant GmbH", 119.0, "invoice", "EUR"))
        self.assertEqual(d["bank_account"], "DE89370400440532013000")
        self.assertEqual(d["customer"], "Kunden AG")
        self.assertEqual(d.totals, {"subtotal": 100.0, "tax": 19.0, "lines": 100.0})

    def test_credit_notes_by_type_or_sign(self):
        self.assertEqual(einvoice.parse_xml(cii(type="381", ref="RE-1"))["kind"], "credit")
        corrected = einvoice.parse_xml(cii(type="384", net="-10.00", tax="-1.90", total="-11.90"))
        self.assertEqual((corrected["kind"], corrected["amount"]), ("credit", 11.9))       # stored positive
        self.assertEqual(einvoice.parse_xml(cii(type="384"))["kind"], "invoice")          # a corrected invoice
        self.assertEqual(einvoice.parse_xml(cii(type="381", ref="RE-1"))["reference"], "RE-1")

    def test_ubl_invoice_and_credit_note(self):
        inv = einvoice.parse_xml(UBL.format(root="Invoice").encode())
        self.assertEqual((inv["id"], inv["vendor"], inv["amount"], inv["kind"], inv["bank_account"]),
                         ("UBL-77", "Leverancier B.V.", 121.0, "invoice", "NL91ABNA0417164300"))
        self.assertEqual(inv.totals["tax"], 21.0)
        cn = einvoice.parse_xml(UBL.format(root="CreditNote").encode())
        self.assertEqual((cn["kind"], cn["reference"]), ("credit", "UBL-70"))

    def test_fatturapa(self):
        one = einvoice.parse_xml(FATTURA.format(bodies=FATTURA_BODY.format(type="TD01", n="2026/7")).encode())
        self.assertEqual((one["id"], one["vendor"], one["amount"], one["kind"], one["customer"]),
                         ("2026/7", "Officine Rossi S.r.l.", 177.0, "invoice", "Mario Bianchi"))
        self.assertEqual(one.totals, {"subtotal": 150.0, "tax": 27.0})                     # summed over the rates
        credit = einvoice.parse_xml(FATTURA.format(bodies=FATTURA_BODY.format(type="TD04", n="NC-1")).encode())
        self.assertEqual((credit["kind"], credit["reference"]), ("credit", "2025/990"))
        batch = FATTURA.format(bodies=FATTURA_BODY.format(type="TD01", n="A") + FATTURA_BODY.format(type="TD01", n="B"))
        ex = read_document("batch.xml", batch.encode())
        self.assertEqual(ex.fields["id"], "A")
        self.assertTrue(any("holds 2 invoices" in w for w in ex.warnings))

    def test_a_standalone_xml_file(self):
        data = cii()
        self.assertEqual(kind_of("x.xml", data), "xml")
        ex = read_document("x.xml", data, "Acme Inc")
        self.assertEqual(ex.fields, {"id": "RE-2026-118", "vendor": "Lieferant GmbH", "date": "2026-03-05",
                                     "amount": 119.0, "bank_account": "DE89370400440532013000"})
        self.assertEqual(set(ex.confidence.values()), {0.99})
        self.assertTrue(ex.method.startswith("E-invoice data"))
        self.assertEqual(ex.missing, [])
        with self.assertRaises(FileProblem):
            read_document("x.xml", b"<?xml version='1.0'?><note>hello</note>")

    def test_your_own_company_as_seller_is_pointed_out(self):
        ex = read_document("x.xml", cii(), "Lieferant GmbH")
        self.assertTrue(any("your own company" in w for w in ex.warnings))


class HostileXmlTests(unittest.TestCase):
    def test_entities_and_dtds_are_refused(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;">]>'
                b"<Invoice>&lol2;</Invoice>")
        external = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
                    b"<Invoice><ID>&e;</ID></Invoice>")
        for data in (bomb, external):
            with self.assertRaises(FileProblem):
                einvoice.parse_xml(data)
            with self.assertRaises(FileProblem):
                read_document("x.xml", data)

    def test_too_large_or_damaged(self):
        with self.assertRaises(FileProblem):
            einvoice.parse_xml(b"<Invoice>" + b" " * (einvoice.MAX_XML + 1) + b"</Invoice>")
        with self.assertRaises(FileProblem) as caught:
            read_document("x.xml", cii()[:-40])
        self.assertIn("damaged", str(caught.exception))

    def test_a_hostile_attachment_in_a_pdf_falls_back_to_the_page(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><CrossIndustryInvoice>&a;</CrossIndustryInvoice>'
        data, truth = pdf_with(bomb)
        ex = read_document("x.pdf", data)
        self.assertEqual(ex.method, "PDF text")
        self.assertEqual(ex.fields["id"], truth["id"])


class EmbeddedDataTests(unittest.TestCase):
    def test_a_pdf_carrying_its_own_data_is_read_from_it(self):
        _, _, doc = next(s for s in doc_samples.sample_docs() if s[0].endswith(".pdf"))
        t = doc.truth()
        xml = cii(id=t["id"], total=f"{t['amount']:.2f}", net=f"{t['amount']:.2f}", tax="0.00")
        data, _ = pdf_with(xml)
        ex = read_document("x.pdf", data)
        self.assertTrue(ex.method.startswith("E-invoice data"))
        self.assertEqual((ex.fields["id"], ex.fields["amount"]), (t["id"], t["amount"]))
        self.assertNotIn("embedded_conflict", ex.totals)
        self.assertEqual(read_document("x.pdf", data, embedded=False).method, "PDF text")

    def test_a_page_that_contradicts_its_data_is_flagged(self):
        """The attack: change the total (or the bank account) on the page a person looks at,
        or swap the attached data, so the two no longer agree."""
        _, _, doc = next(s for s in doc_samples.sample_docs() if s[0].endswith(".pdf"))
        t = doc.truth()
        data, _ = pdf_with(cii(id=t["id"], total="9999.00"))
        ex = read_document("x.pdf", data)
        self.assertIn("contradicts itself", ex.warnings[0])
        self.assertIn("9,999.00", ex.warnings[0])
        self.assertEqual(totals_issue(ex.fields["amount"], ex.totals), ex.warnings[0])   # so it becomes a flag


if __name__ == "__main__":
    unittest.main()
