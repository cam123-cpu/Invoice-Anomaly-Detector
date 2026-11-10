"""Anomaly detection: transparent rules plus an Isolation Forest.

Rules are explainable and auditable; the ML model catches unusual combinations the
rules don't anticipate. Output is a list of signals for HUMAN review, not proof of fraud.
"""
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as Date
from statistics import median


@dataclass(frozen=True)
class Invoice:
    id: str
    vendor: str
    amount: float
    date: str  # ISO yyyy-mm-dd
    bank_idx: str | None = None  # blind index of the bank account, never the account itself


@dataclass(frozen=True)
class Anomaly:
    invoice_id: str
    rule: str
    severity: str  # "high" | "medium" | "low"
    detail: str


SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def find_duplicates(invoices: list[Invoice]) -> list[Anomaly]:
    """Same vendor + amount + date as an earlier invoice."""
    seen: dict[tuple, str] = {}
    out = []
    for inv in invoices:
        key = (inv.vendor, inv.amount, inv.date)
        if key in seen:
            out.append(Anomaly(inv.id, "duplicate", "high", f"duplicate of {seen[key]}"))
        else:
            seen[key] = inv.id
    return out


def find_amount_outliers(invoices: list[Invoice], min_history: int = 5,
                         threshold: float = 3.5) -> list[Anomaly]:
    """Robust z-score (median/MAD) per vendor. Unlike mean/stdev, one huge invoice
    can't hide itself by inflating the baseline."""
    by_vendor: dict[str, list[Invoice]] = defaultdict(list)
    for inv in invoices:
        by_vendor[inv.vendor].append(inv)
    out = []
    for group in by_vendor.values():
        if len(group) < min_history:
            continue
        amounts = [i.amount for i in group]
        med = median(amounts)
        mad = median(abs(a - med) for a in amounts)
        for inv in group:
            if mad == 0:
                if inv.amount > 3 * med:
                    out.append(Anomaly(inv.id, "amount_outlier", "medium",
                                       f"amount {inv.amount:,.2f} vs vendor median {med:,.2f}"))
                continue
            z = 0.6745 * (inv.amount - med) / mad
            if abs(z) > threshold:
                sev = "high" if abs(z) > 7 else "medium"
                out.append(Anomaly(inv.id, "amount_outlier", sev,
                                   f"amount {inv.amount:,.2f} vs vendor median {med:,.2f} "
                                   f"(robust z = {z:.1f})"))
    return out


def find_shared_bank_accounts(invoices: list[Invoice]) -> list[Anomaly]:
    """One bank account paid out to several vendors: a classic fake-vendor signal.
    Only the minority vendor's invoices are flagged, to limit noise."""
    by_bank: dict[str, dict[str, list[Invoice]]] = defaultdict(lambda: defaultdict(list))
    for inv in invoices:
        if inv.bank_idx:
            by_bank[inv.bank_idx][inv.vendor].append(inv)
    out = []
    for vendors in by_bank.values():
        if len(vendors) < 2:
            continue
        top = max(len(v) for v in vendors.values())
        primaries = [name for name, invs in vendors.items() if len(invs) == top]
        for name, invs in vendors.items():
            if len(primaries) == 1 and name == primaries[0]:
                continue
            for inv in invs:
                out.append(Anomaly(inv.id, "shared_bank_account", "high",
                                   f"bank account also used by {len(vendors) - 1} other vendor(s)"))
    return out


def find_round_amounts(invoices: list[Invoice]) -> list[Anomaly]:
    return [Anomaly(i.id, "round_amount", "low", f"suspiciously round amount {i.amount:,.2f}")
            for i in invoices if i.amount >= 1000 and i.amount % 1000 == 0]


def find_weekend_invoices(invoices: list[Invoice]) -> list[Anomaly]:
    return [Anomaly(i.id, "weekend_invoice", "low", f"dated on a weekend ({i.date})")
            for i in invoices if Date.fromisoformat(i.date).weekday() >= 5]


def find_ml_outliers(invoices: list[Invoice], min_rows: int = 30,
                     contamination: float = 0.03) -> list[Anomaly]:
    """Isolation Forest over amount, amount vs vendor median, weekday and day of month.
    Skipped on small datasets, where it would mostly produce noise."""
    if len(invoices) < min_rows:
        return []
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return []
    vendor_median = {v: median(i.amount for i in invoices if i.vendor == v)
                     for v in {i.vendor for i in invoices}}
    rows = []
    for inv in invoices:
        d = Date.fromisoformat(inv.date)
        rows.append([math.log1p(inv.amount), math.log(inv.amount / vendor_median[inv.vendor]),
                     d.weekday(), d.day])
    labels = IsolationForest(n_estimators=200, contamination=contamination,
                             random_state=0).fit_predict(np.array(rows))
    return [Anomaly(inv.id, "ml_outlier", "medium",
                    "unusual mix of amount, vendor history and timing (Isolation Forest)")
            for inv, label in zip(invoices, labels) if label == -1]


def detect(invoices: list[Invoice], use_ml: bool = True) -> list[Anomaly]:
    found = [
        *find_duplicates(invoices),
        *find_amount_outliers(invoices),
        *find_shared_bank_accounts(invoices),
        *find_round_amounts(invoices),
        *find_weekend_invoices(invoices),
    ]
    if use_ml:
        found += find_ml_outliers(invoices)
    return sorted(found, key=lambda a: (SEVERITY_RANK[a.severity], a.invoice_id, a.rule))


def risk_scores(anomalies: list[Anomaly]) -> dict[str, int]:
    """Sum of severity weights per invoice, for ranking a review queue."""
    scores: dict[str, int] = defaultdict(int)
    for a in anomalies:
        scores[a.invoice_id] += SEVERITY_WEIGHT[a.severity]
    return dict(sorted(scores.items(), key=lambda kv: -kv[1]))
