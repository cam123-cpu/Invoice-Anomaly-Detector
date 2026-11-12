"""What invoices call things in German, French, Dutch, Spanish and Italian.

documents.py reads a form by finding a label and taking the value beside it. Its own
tables are English; this module adds the same labels in five more languages, so that
"Rechnungsnummer", "Date de facture" or "Totaal te betalen" are found the same way as
"Invoice number", "Invoice date" and "Total due".

Labels are written as people print them; they are normalised (lower case, accents
removed, punctuation to spaces) when the tables are built. A label ending in ":" only
counts when it stands alone or is followed by a colon: "Lieferant:" is a label, while
"Lieferant GmbH" is a company called Lieferant.

Adding a language is adding its words here; nothing else changes.
"""
from __future__ import annotations

# (label, strength). 3: says exactly what it is. 2: usually. 1: could be something else.
LABELS = {
    "id": [
        # German
        ("Rechnungsnummer", 3), ("Rechnungsnr", 3), ("Rechnungs-Nr", 3), ("Rechnung Nr", 3), ("Rechnung Nummer", 3),
        ("Rechnungs-Nummer", 3), ("Rechnung No", 3), ("Belegnummer", 3), ("Belegnr", 3), ("Beleg-Nr", 3), ("Beleg-Nummer", 3),
        ("Gutschriftsnummer", 3), ("Gutschrift Nr", 3), ("Gutschrifts-Nr", 3), ("Re.-Nr", 3), ("Re-Nr", 3), ("RNr", 2),
        ("Rechnung", 1), ("Gutschrift", 1), ("Nummer", 1), ("Nr", 1), ("Beleg", 1),
        # French
        ("Numéro de facture", 3), ("N° de facture", 3), ("N° facture", 3), ("Numéro facture", 3), ("Facture n°", 3),
        ("Facture numéro", 3), ("Facture num", 3), ("Référence facture", 3), ("Réf. facture", 3),
        ("Numéro d'avoir", 3), ("N° d'avoir", 3), ("Avoir n°", 3), ("Numéro de pièce", 2), ("N° de pièce", 2),
        ("Facture", 1), ("Avoir", 1), ("Numéro", 1),
        # Dutch
        ("Factuurnummer", 3), ("Factuurnr", 3), ("Factuur nr", 3), ("Factuur nummer", 3), ("Factuur no", 3),
        ("Creditnota nummer", 3), ("Creditnotanummer", 3), ("Factuur", 1), ("Creditnota", 1),
        # Spanish
        ("Número de factura", 3), ("Nº de factura", 3), ("Nº factura", 3), ("Número factura", 3), ("Factura nº", 3),
        ("Factura número", 3), ("Factura num", 3), ("Núm. factura", 3), ("Factura", 1),
        # Italian
        ("Numero fattura", 3), ("Numero della fattura", 3), ("Fattura n°", 3), ("Fattura nr", 3), ("Fattura numero", 3),
        ("N° fattura", 3), ("Nr fattura", 3), ("Numero documento", 2), ("Fattura", 1), ("Numero", 1),
    ],
    "date": [
        ("Rechnungsdatum", 3), ("Rechnungs-Datum", 3), ("Belegdatum", 3), ("Beleg-Datum", 3), ("Ausstellungsdatum", 3), ("Datum der Rechnung", 3),
        ("Date de facture", 3), ("Date de la facture", 3), ("Date facture", 3), ("Date d'émission", 3), ("Date de facturation", 3),
        ("Émise le", 3), ("Emise le", 3), ("Factuurdatum", 3), ("Factuur datum", 3), ("Datum factuur", 3),
        ("Fecha de factura", 3), ("Fecha factura", 3), ("Fecha de emisión", 3), ("Fecha emisión", 3), ("Fecha de expedición", 3),
        ("Data fattura", 3), ("Data della fattura", 3), ("Data emissione", 3), ("Data di emissione", 3), ("Data documento", 2),
        ("Datum", 2), ("Fecha", 2), ("Data", 2),
    ],
    "amount": [
        ("Gesamtbetrag", 3), ("Rechnungsbetrag", 3), ("Endbetrag", 3), ("Zahlbetrag", 3), ("Gesamtsumme", 3), ("Endsumme", 3),
        ("Bruttobetrag", 3), ("Bruttosumme", 3), ("Summe brutto", 3), ("Gesamt brutto", 3), ("Betrag brutto", 3),
        ("Rechnungssumme", 3), ("Zu zahlen", 3), ("Zu zahlender Betrag", 3), ("Zahlbar", 2), ("Fälliger Betrag", 3),
        ("Rechnungsendbetrag", 3), ("Gesamt", 2), ("Summe", 2), ("Betrag", 1),
        ("Total TTC", 3), ("Montant TTC", 3), ("Montant total TTC", 3), ("Net à payer", 3), ("Somme à payer", 3),
        ("Total à payer", 3), ("Solde à payer", 3), ("Reste à payer", 3), ("Montant à payer", 3), ("Montant total", 3),
        ("Total général", 3), ("Total facture", 3), ("Montant", 1),
        ("Totaal te betalen", 3), ("Te betalen", 3), ("Totaalbedrag", 3), ("Factuur totaal", 3), ("Factuurtotaal", 3),
        ("Factuurbedrag", 3), ("Totaal incl", 3), ("Totaal inclusief", 3), ("Totaal", 2), ("Bedrag", 1),
        ("Total a pagar", 3), ("Importe total", 3), ("Total factura", 3), ("Importe", 1),
        ("Totale fattura", 3), ("Totale documento", 3), ("Totale da pagare", 3), ("Netto a pagare", 3),
        ("Importo totale", 3), ("Totale complessivo", 3), ("Totale", 2), ("Importo", 1),
    ],
    "subtotal": [
        ("Nettobetrag", 3), ("Summe netto", 3), ("Gesamt netto", 3), ("Zwischensumme", 3), ("Nettosumme", 3),
        ("Gesamtbetrag netto", 3), ("Betrag netto", 3), ("Warenwert", 2), ("Netto", 2),
        ("Total HT", 3), ("Montant HT", 3), ("Total hors taxe", 3), ("Total hors taxes", 3), ("Sous-total", 3),
        ("Montant total HT", 3), ("Base HT", 2),
        ("Subtotaal", 3), ("Totaal excl", 3), ("Totaal exclusief", 3), ("Exclusief BTW", 3), ("Excl. BTW", 3),
        ("Base imponible", 3), ("Imponibile", 3), ("Totale imponibile", 3),
    ],
    "tax": [
        ("MwSt", 3), ("USt", 3), ("Umsatzsteuer", 3), ("Mehrwertsteuer", 3), ("zzgl. MwSt", 3), ("zzgl. USt", 3),
        ("Steuerbetrag", 3), ("Summe MwSt", 3), ("Summe USt", 3),
        ("TVA", 3), ("Total TVA", 3), ("Montant TVA", 3), ("Total taxes", 3), ("Dont TVA", 3),
        ("BTW", 3), ("Totaal BTW", 3), ("BTW bedrag", 3), ("IVA", 3), ("Total IVA", 3), ("Totale IVA", 3), ("Imposta", 2),
    ],
    "shipping": [("Versandkosten", 3), ("Versand", 2), ("Frais de port", 3), ("Frais de livraison", 3),
                 ("Verzendkosten", 3), ("Gastos de envío", 3), ("Spese di spedizione", 3), ("Porto", 2)],
    "discount": [("Rabatt", 3), ("Remise", 3), ("Korting", 3), ("Descuento", 3), ("Sconto", 3)],
    "paid": [("Anzahlung", 3), ("Bereits bezahlt", 3), ("Bereits gezahlt", 3), ("Acompte", 3), ("Déjà réglé", 3),
             ("Déjà payé", 3), ("Aanbetaling", 3), ("Reeds betaald", 3), ("Pagado", 2), ("Anticipo", 3), ("Acconto", 3)],
    "bank_account": [("Kontonummer", 2), ("Konto-Nr", 2), ("Kto-Nr", 2), ("Kto.-Nr", 2), ("Rekeningnummer", 2),
                     ("Numéro de compte", 2), ("N° de compte", 2), ("Número de cuenta", 2), ("Numero conto", 2)],
    "payable_to": [("Verkäufer:", 3), ("Lieferant:", 3), ("Leistungserbringer:", 3), ("Rechnungssteller:", 3),
                   ("Aussteller:", 3), ("Rechnungsersteller:", 3), ("Rechnungsaussteller:", 3), ("Absender:", 3), ("Vendeur:", 3), ("Fournisseur:", 3), ("Émetteur:", 3),
                   ("Leverancier:", 3), ("Verkoper:", 3), ("Proveedor:", 3), ("Emisor:", 3), ("Vendedor:", 3),
                   ("Fornitore:", 3), ("Cedente/prestatore:", 3), ("Mittente:", 3)],
    "customer": [("Käufer", 3), ("Käufer/Leistungsempfänger", 3), ("Kunde", 2), ("Rechnungsempfänger", 3),
                 ("Leistungsempfänger", 3), ("Rechnungsadresse", 3), ("Rechnungsanschrift", 3), ("Rechnung an", 3),
                 ("Lieferadresse", 2), ("Lieferanschrift", 2), ("Empfänger", 2),
                 ("Facturé à", 3), ("Adresse de facturation", 3), ("Adresse de livraison", 2), ("Destinataire", 2),
                 ("Livré à", 2), ("Acheteur", 3), ("Klant", 2), ("Factuuradres", 3), ("Afleveradres", 2), ("Koper", 3),
                 ("Cliente", 2), ("Facturar a", 3), ("Destinatario", 2), ("Comprador", 3),
                 ("Cessionario/committente", 3), ("Spett.le", 2)],
}

# Phrases that start like one of the labels above but mean something else.
NOT_LABEL = {
    "id": ("Rechnungsdatum", "Rechnungs-Datum", "Rechnungsbetrag", "Rechnung an", "Rechnungsadresse", "Rechnungsanschrift",
           "Rechnungsempfänger", "Rechnung gemäß", "Rechnung für", "Rechnung über", "Rechnungssumme", "Rechnungswährung",
           "Rechnungsendbetrag", "Rechnungssteller", "Rechnung Seite", "Nr. Kunde", "Nummer des Kunden",
           "Facture du", "Facture à", "Facture en", "Facture pour", "Numéro de client", "Numéro client", "Numéro de TVA",
           "Numéro de commande", "Numéro de téléphone", "Numéro de ligne", "Numéro de compte", "Numéro de pièce jointe",
           "Factuurdatum", "Factuuradres", "Factuur datum", "Factuur totaal", "Factuurtotaal", "Factuurbedrag",
           "Factura a", "Factura para", "Fattura del", "Fattura a", "Numero cliente", "Numero ordine", "Numero di telefono",
           "Numero conto", "Numero de cliente", "Numero de pedido", "Numero de cuenta", "Nummer der Bestellung"),
    "date": ("Date d'échéance", "Date échéance", "Date limite", "Date de livraison", "Date de commande", "Date de paiement",
             "Date de règlement", "Date d'expédition", "Date de valeur", "Datum der Lieferung", "Datum Lieferung",
             "Fecha de vencimiento", "Fecha vencimiento", "Fecha de entrega", "Fecha de pago", "Fecha de pedido",
             "Data scadenza", "Data di scadenza", "Data consegna", "Data di consegna", "Data ordine", "Data pagamento"),
    "amount": ("Summe netto", "Gesamt netto", "Gesamtbetrag netto", "Betrag netto", "Summe MwSt", "Summe USt", "Gesamt MwSt",
               "Summe Rabatt", "Gesamtgewicht", "Gesamtmenge", "Summe Positionen", "Total HT", "Montant HT",
               "Montant total HT", "Total TVA", "Montant TVA", "Total taxes", "Total hors", "Total remise", "Montant remise",
               "Total facture HT", "Totaal excl", "Totaal exclusief", "Totaal BTW", "Totaal korting", "Total IVA",
               "Total base", "Totale IVA", "Totale imponibile", "Importe IVA", "Importo IVA", "Total descuento",
               "Totale sconto", "Montant en lettres", "Betrag in Worten"),
    "tax": ("USt-Id", "USt.-Id", "USt-IdNr", "USt. Id", "USt Id", "UStId", "MwSt-Nr", "MwSt.-Nr", "TVA intra", "TVA n°",
            "TVA :", "TVA non applicable", "BTW nummer", "BTW-nummer", "BTW nr", "BTW no", "BTW NL", "BTW BE", "BTW %",
            "IVA n°", "TVA %", "MwSt %", "MwSt-Satz", "USt-Satz", "Umsatzsteuer-Id", "Umsatzsteuer-Identifikation",
            "Umsatzsteueridentifikation", "Umsatzsteuer ID"),
    "customer": ("Kundennummer", "Kunden-Nr", "Kundennr", "Client n°", "Klantnummer", "Cliente n"),
    "subtotal": ("Netto Kasse", "Netto innerhalb", "Netto zahlbar"),
}

# Month names -> month number. Accents are removed and the text lower-cased before lookup.
MONTHS = {
    # German
    "januar": 1, "janner": 1, "jan": 1, "februar": 2, "feb": 2, "marz": 3, "maerz": 3, "mrz": 3, "april": 4, "apr": 4,
    "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12,
    # French
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "fev": 2, "mars": 3, "avril": 4, "avr": 4, "juin": 6,
    "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "octobre": 10, "oct": 10, "novembre": 11, "decembre": 12, "dec": 12,
    # Dutch
    "januari": 1, "februari": 2, "maart": 3, "mrt": 3, "mei": 5, "augustus": 8,
    # Spanish
    "enero": 1, "ene": 1, "febrero": 2, "marzo": 3, "abril": 4, "abr": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "ago": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12, "dic": 12,
    # Italian
    "gennaio": 1, "gen": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "mag": 5, "giugno": 6, "giu": 6, "luglio": 7,
    "lug": 7, "settembre": 9, "set": 9, "ottobre": 10, "ott": 10, "dicembre": 12,
    # English
    "january": 1, "february": 2, "march": 3, "mar": 3, "may": 5, "june": 6, "july": 7, "october": 10, "december": 12,
}
ENGLISH_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                  "November", "December")

# Words that name the document. A line containing one is a title ("Rechnung", "Kopie
# factuur", "Facture d'acompte"), never the vendor's name.
DOCUMENT_WORDS = ("rechnung", "handelsrechnung", "gutschrift", "rechnungskorrektur", "lieferschein", "bestellung",
                  "mahnung", "angebot", "facture", "avoir", "justificatif", "devis", "bon de commande", "factuur",
                  "creditnota", "pakbon", "offerte", "factura", "albaran", "presupuesto", "fattura", "nota di credito",
                  "preventivo", "stylesheet", "beispiel", "muster rechnung", "duplicata", "kopie", "copie", "original")

# Seeing any of these means the document is not English, so its dates are day-first.
FOREIGN_MARKERS = ("rechnung", "rechnungsnummer", "rechnungsdatum", "mwst", "ust", "umsatzsteuer", "gesamtbetrag",
                   "kunde", "lieferant", "facture", "tva", "ttc", "montant", "siret", "factuur", "btw", "totaal",
                   "factuurnummer", "factura", "iva", "importe", "fecha", "fattura", "imponibile", "totale", "partita")

# Words beside a date that say it is NOT the invoice date (due, delivery, order, payment).
OTHER_DATE_WORDS = ("due", "delivery", "ship", "order", "paid", "period", "expir", "fallig", "lieferdatum", "liefertermin",
                    "lieferung", "liefer und", "leistung", "bestell", "zahlung", "zahlbar", "echeance", "livraison",
                    "commande", "paiement", "reglement", "limite", "verval", "leverdatum", "levering", "betaal",
                    "vencimiento", "entrega", "pedido", "pago", "scadenza", "consegna", "ordine", "pagamento",
                    "prelevement", "valuta")

# Legal forms: a line ending in (or containing) one is a company's name.
LEGAL_FORMS = ("gmbh", "ag", "kg", "ug", "ohg", "gbr", "e.k.", "ek", "e.v.", "se", "kgaa", "mbh", "a.g.",
               "sarl", "sas", "sa", "s.a.", "eurl", "sasu", "sci", "snc", "s.a.s.", "s.a.r.l.",
               "b.v.", "bv", "n.v.", "nv", "v.o.f.", "vof",
               "s.l.", "sl", "s.l.u.", "s.a.u.", "srl", "s.r.l.", "spa", "s.p.a.", "sas", "snc",
               "ltd", "ltd.", "limited", "inc", "inc.", "llc", "plc", "corp", "corp.", "co.", "pvt", "pty", "oy", "ab",
               "aps", "a/s", "as", "sp. z o.o.")
