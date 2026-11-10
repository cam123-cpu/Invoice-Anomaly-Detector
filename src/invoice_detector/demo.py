"""Synthetic invoices with known, injected anomalies, for demos and tests."""
import random
from datetime import date, timedelta

VENDORS = ["Acme Supplies", "Globex", "Initech", "Umbrella Ltd", "Hooli"]


def generate_demo(seed: int = 0, per_vendor: int = 40):
    """Returns (rows, injected). rows are dicts; injected maps rule -> invoice id."""
    rng = random.Random(seed)
    days, d = [], date(2026, 1, 5)  # a Monday
    while len(days) < per_vendor + 5:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    banks = {v: f"ACCT{idx:02d}{rng.randrange(10**10):010d}" for idx, v in enumerate(VENDORS)}
    rows, n = [], 0

    def add(vendor, amount, when, bank):
        nonlocal n
        n += 1
        rows.append({"id": f"INV-{n:04d}", "vendor": vendor, "amount": round(amount, 2),
                     "date": when.isoformat(), "bank_account": bank})
        return rows[-1]["id"]

    for vi, vendor in enumerate(VENDORS):
        for i in range(per_vendor):
            amount = max(50.0, rng.gauss(500 + vi * 100, 40))
            add(vendor, amount, days[i], banks[vendor])

    first = rows[0]
    injected = {
        "duplicate": add(first["vendor"], first["amount"], date.fromisoformat(first["date"]),
                         first["bank_account"]),
        "amount_outlier": add("Globex", 25000.37, days[41], banks["Globex"]),
        "shared_bank_account": add("Hooli", 640.12, days[42], banks["Umbrella Ltd"]),
        "round_amount": add("Initech", 9000.00, days[43], banks["Initech"]),
        "weekend_invoice": add("Hooli", 710.55, date(2026, 3, 7), banks["Hooli"]),  # a Saturday
    }
    return rows, injected
