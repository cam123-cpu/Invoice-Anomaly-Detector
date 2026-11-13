"""Synthetic invoices with known, injected anomalies, for demos, tests and evaluation.

Nothing here is real data. Amounts are log-normal per vendor, one vendor is a fixed
monthly subscription, and each injected anomaly is recorded so results can be scored.
"""
import math
import random
from datetime import date, timedelta

# name, typical amount, spread (sigma of log amount), days between invoices
VENDORS = [
    ("Acme Supplies", 480.0, 0.18, 7),
    ("Globex Corp", 1250.0, 0.22, 7),
    ("Initech LLC", 3200.0, 0.15, 14),
    ("Umbrella Ltd", 760.0, 0.25, 7),
    ("Hooli Cloud", 2400.0, 0.0, 30),      # fixed monthly subscription
    ("Stark Freight", 5200.0, 0.20, 14),
    ("Wayne Office", 150.0, 0.30, 5),
    ("Wonka Catering", 340.0, 0.35, 10),
]
START = date(2026, 1, 5)  # a Monday
DAYS = 270
DEMO_END = (START + timedelta(days=DAYS)).isoformat()   # "today" when scoring the demo


def _weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def generate_demo(seed: int = 0):
    """Returns (rows, injected). rows are dicts ready for the database;
    injected maps invoice id -> set of rules that should fire on it."""
    rng = random.Random(seed)
    rows: list[dict] = []
    injected: dict[str, set] = {}
    banks = {name: f"ACCT{idx:02d}{rng.randrange(10**10):010d}" for idx, (name, *_) in enumerate(VENDORS)}

    def add(vendor, amount, when, bank, rules=None, kind="invoice", inv_id=None):
        amount = round(amount, 2)
        if rules is None and amount % 1000 == 0:
            amount += 0.37  # keep the clean data free of accidental round amounts
        inv_id = inv_id or f"{'CN' if kind == 'credit' else 'INV'}-{len(rows) + 1:04d}"
        rows.append({"id": inv_id, "vendor": vendor, "amount": amount,
                     "date": when.isoformat(), "bank_account": bank, "kind": kind})
        if rules:
            injected[inv_id] = set(rules)
        return rows[-1]

    for name, typical, sigma, cadence in VENDORS:
        d = START + timedelta(days=rng.randrange(cadence))
        while (d - START).days < DAYS:
            noise = max(-3 * sigma, min(3 * sigma, rng.gauss(0, sigma)))
            add(name, typical * math.exp(noise), _weekday(d + timedelta(days=rng.randrange(3))),
                banks[name])
            d += timedelta(days=cadence)

    clean = list(rows)
    late = [r for r in clean if r["date"] >= (START + timedelta(days=120)).isoformat()]

    def pick(vendor):
        return rng.choice([r for r in late if r["vendor"] == vendor])

    def on(vendor, offset=0):
        return _weekday(date.fromisoformat(pick(vendor)["date"]) + timedelta(days=offset))

    # 1. exact duplicate
    src = pick("Acme Supplies")
    add(src["vendor"], src["amount"], date.fromisoformat(src["date"]), src["bank_account"],
        {"duplicate"})
    # 2. same invoice re-submitted a few days later
    src = pick("Globex Corp")
    add(src["vendor"], src["amount"], _weekday(date.fromisoformat(src["date"]) + timedelta(days=4)),
        src["bank_account"], {"near_duplicate"})
    # 3. far above the vendor's norm (and over the approval threshold)
    add("Umbrella Ltd", 14860.37, on("Umbrella Ltd", 2), banks["Umbrella Ltd"], {"amount_outlier"})
    # 4. established vendor, brand-new bank account
    add("Initech LLC", 3150.42, on("Initech LLC", 3), "ACCT90" + "7" * 10, {"bank_account_change"})
    # 5. one vendor paid into another vendor's account
    add("Wonka Catering", 362.18, on("Wonka Catering", 2), banks["Wayne Office"],
        {"shared_bank_account", "bank_account_change"})
    # 6. a bill split into three to stay under the 10,000 approval threshold
    stark_days = [date.fromisoformat(r["date"]) for r in clean if r["vendor"] == "Stark Freight"]
    day = START + timedelta(days=150)
    while day.weekday() != 1 or any(abs((day - s).days) <= 6 for s in stark_days):
        day += timedelta(days=1)
    for k, amount in enumerate((3912.40, 3405.15, 3651.80)):
        add("Stark Freight", amount, day + timedelta(days=k), banks["Stark Freight"], {"split_invoice"})
    # 7. look-alike vendor name with its own bank account
    for offset in (1, 9):
        add("Acme Suppiles", 495.60 + offset, on("Acme Supplies", offset), "ACCT91" + "3" * 10,
            {"similar_vendor_name"})
    # 8. round amount
    add("Initech LLC", 4000.00, on("Initech LLC", 4), banks["Initech LLC"], {"round_amount"})
    # 9. weekend date
    saturday = START + timedelta(days=152)
    saturday += timedelta(days=(5 - saturday.weekday()) % 7)
    add("Wayne Office", 163.25, saturday, banks["Wayne Office"], {"weekend_invoice"})
    # 10. just under the approval threshold
    add("Stark Freight", 9890.00, _weekday(day + timedelta(days=30)), banks["Stark Freight"],
        {"below_threshold"})
    # 11. brand-new vendor, large first invoice
    add("Vandelay Industries", 18400.55, on("Globex Corp", 1), "ACCT92" + "5" * 10,
        {"new_vendor_large"})

    # 12. an invoice billed at the wrong amount, cancelled by a credit note, then re-issued for
    #     the same amount two days later. The pair is flagged; the re-issue must NOT be called a
    #     duplicate of the invoice it replaces, but waits (lightly flagged) until the credit is confirmed.
    when = on("Globex Corp", 6)
    add("Globex Corp", 1842.75, when, banks["Globex Corp"], {"credited_invoice"})
    add("Globex Corp", 1842.75, _weekday(when + timedelta(days=2)), None, {"matched_credit"}, kind="credit")
    add("Globex Corp", 1842.75, _weekday(when + timedelta(days=4)), banks["Globex Corp"], {"reissued_invoice"})
    # 13. an ordinary small credit (a returned item): clean, it just reduces what is owed
    add("Wayne Office", 38.20, on("Wayne Office", 5), None, kind="credit")
    # 14. a credit note from a company that has never billed anything
    add("Soylent Partners", 2600.00, on("Initech LLC", 2), None, {"unmatched_credit"}, kind="credit")
    # 15. an invoice sent again three weeks later with its number typed differently and the
    #     amount nudged, so it isn't an exact or near duplicate
    src = pick("Wayne Office")
    add(src["vendor"], src["amount"] + 12.50, _weekday(date.fromisoformat(src["date"]) + timedelta(days=21)),
        src["bank_account"], {"duplicate_number"}, inv_id=src["id"].replace("-", ""))
    # 16. a vendor whose invoices to us are numbered without gaps (nobody else is billed)
    first = START + timedelta(days=60)
    for k in range(5):
        add("Pinnacle Advisory", 1180.00 + 37.15 * k, _weekday(first + timedelta(days=30 * k)), "ACCT93" + "1" * 10,
            {"sequential_numbers"} if k == 4 else None, inv_id=f"PA-{2041 + k}")
    # 17. dated in the future (relative to the end of the demo period)
    add("Initech LLC", 2875.40, START + timedelta(days=DAYS + 45), banks["Initech LLC"], {"future_date"})

    return rows, injected
