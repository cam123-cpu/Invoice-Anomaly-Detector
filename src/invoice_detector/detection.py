"""Anomaly detection: transparent rules, an Isolation Forest, and a combined risk score.

Rules are explainable and auditable; the ML model catches unusual combinations the rules
don't anticipate. Output is a ranked list of signals for HUMAN review, not proof of fraud.
"""
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date as Date
from difflib import SequenceMatcher
from statistics import median


@dataclass(frozen=True)
class Invoice:
    id: str
    vendor: str
    amount: float
    date: str  # ISO yyyy-mm-dd
    bank_idx: str | None = None  # blind index of the bank account, never the account itself
    totals_issue: str | None = None  # read from the invoice document: its own arithmetic doesn't add up
    kind: str = "invoice"            # "invoice", or "credit" for a credit note (amount stays positive)


@dataclass(frozen=True)
class Anomaly:
    invoice_id: str
    rule: str
    severity: str  # "high" | "medium" | "low"
    detail: str


@dataclass(frozen=True)
class DetectionConfig:
    approval_threshold: float = 10_000.0  # amounts at/above this need extra sign-off
    below_threshold_pct: float = 0.05     # "just under" = within 5% below the threshold
    near_duplicate_days: int = 10
    split_window_days: int = 3
    outlier_z: float = 4.0                # robust z-score on log amounts
    min_history: int = 8                  # invoices a vendor needs before outlier checks
    name_similarity: float = 0.85
    new_vendor_max_invoices: int = 2
    new_vendor_multiple: float = 5.0      # x the overall median amount
    use_ml: bool = True
    ml_min_rows: int = 50
    ml_sensitivity: float = 6.0           # lower = more ML flags


DEFAULT_CONFIG = DetectionConfig()
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

# How strongly each rule suggests a problem (0-1). Combined with a noisy-OR in risk_scores.
RULE_WEIGHT = {
    "duplicate": 0.80,
    "bank_account_change": 0.80,
    "shared_bank_account": 0.70,
    "amount_outlier": 0.55,
    "totals_mismatch": 0.45,
    "credited_invoice": 0.65,
    "matched_credit": 0.65,
    "unmatched_credit": 0.45,
    "duplicate_credit": 0.5,
    "split_invoice": 0.50,
    "near_duplicate": 0.50,
    "similar_vendor_name": 0.50,
    "new_vendor_large": 0.40,
    "ml_outlier": 0.35,
    "below_threshold": 0.20,
    "round_amount": 0.15,
    "weekend_invoice": 0.10,
}

# Plain-English name and explanation of each rule, for people (web app, Excel output).
RULE_INFO = {
    "duplicate": ("Duplicate", "Same vendor, amount and date as an earlier invoice"),
    "bank_account_change": ("New bank account", "First payment to a different bank account for an established vendor"),
    "shared_bank_account": ("Shared bank account", "The same bank account is used by another vendor"),
    "amount_outlier": ("Unusual amount", "Far above this vendor's normal invoice amount"),
    "split_invoice": ("Possible split", "Several invoices in a few days that together pass the approval limit"),
    "near_duplicate": ("Near-duplicate", "Same vendor and amount a few days apart"),
    "similar_vendor_name": ("Look-alike vendor", "Vendor name is almost identical to another vendor's"),
    "new_vendor_large": ("Large first invoice", "Large invoice from a vendor with almost no history"),
    "ml_outlier": ("Unusual pattern", "Statistical model found an unusual mix of amount, timing and vendor history"),
    "below_threshold": ("Just under limit", "Amount is just below the approval threshold"),
    "round_amount": ("Round amount", "Suspiciously round figure"),
    "weekend_invoice": ("Weekend date", "Invoice is dated on a weekend"),
    "totals_mismatch": ("Totals don't add up", "The invoice's line items, subtotal, tax and total don't agree"),
    "credited_invoice": ("Cancelled by a credit note", "A credit note for the same vendor and amount cancels this invoice"),
    "matched_credit": ("Cancels an invoice", "This credit note cancels a specific invoice in full"),
    "unmatched_credit": ("Credit with no matching invoices", "Credit note from a vendor with nothing, or less, billed"),
    "duplicate_credit": ("Duplicate credit note", "Same vendor, amount and date as another credit note"),
}


def _day(inv: Invoice) -> Date:
    return Date.fromisoformat(inv.date)


def _by_vendor(invoices: list[Invoice]) -> dict[str, list[Invoice]]:
    groups: dict[str, list[Invoice]] = defaultdict(list)
    for inv in sorted(invoices, key=lambda i: (i.date, i.id)):
        groups[inv.vendor].append(inv)
    return groups


def _robust_log_z(amount: float, log_amounts: list[float]) -> float:
    """Robust z-score of log(amount) against a vendor's history (median / MAD).
    The MAD is floored at 5% so very consistent vendors don't trigger on tiny changes,
    and one huge invoice can't hide itself by inflating the baseline."""
    med = median(log_amounts)
    mad = max(median(abs(x - med) for x in log_amounts), 0.05)
    return 0.6745 * (math.log(amount) - med) / mad


# ---------------------------------------------------------------- rules
def find_duplicates(invoices: list[Invoice]) -> list[Anomaly]:
    """Same vendor + amount + date as an earlier invoice."""
    seen: dict[tuple, str] = {}
    out = []
    for inv in sorted(invoices, key=lambda i: (i.date, i.id)):
        key = (inv.vendor, inv.amount, inv.date)
        if key in seen:
            out.append(Anomaly(inv.id, "duplicate", "high", f"duplicate of {seen[key]}"))
        else:
            seen[key] = inv.id
    return out


def find_near_duplicates(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    """Same vendor + amount a few days apart (a re-submitted invoice with a new date).
    Monthly fixed-price subscriptions are further apart than the window, so they pass."""
    groups: dict[tuple, list[Invoice]] = defaultdict(list)
    for inv in sorted(invoices, key=lambda i: (i.date, i.id)):
        groups[(inv.vendor, inv.amount)].append(inv)
    out = []
    for group in groups.values():
        for prev, cur in zip(group, group[1:]):
            gap = (_day(cur) - _day(prev)).days
            if 0 < gap <= cfg.near_duplicate_days:
                out.append(Anomaly(cur.id, "near_duplicate", "medium",
                                   f"same vendor and amount as {prev.id}, {gap} day(s) later"))
    return out


def find_amount_outliers(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    """Amount far above the vendor's own history."""
    out = []
    for group in _by_vendor(invoices).values():
        if len(group) < cfg.min_history:
            continue
        logs = [math.log(i.amount) for i in group]
        typical = math.exp(median(logs))
        for inv in group:
            z = _robust_log_z(inv.amount, logs)
            if z > cfg.outlier_z:
                sev = "high" if z > 2 * cfg.outlier_z else "medium"
                out.append(Anomaly(inv.id, "amount_outlier", sev,
                                   f"amount {inv.amount:,.2f} is {inv.amount / typical:.1f}x the "
                                   f"vendor's typical {typical:,.2f} (robust z = {z:.1f})"))
    return out


def find_bank_account_changes(invoices: list[Invoice]) -> list[Anomaly]:
    """First payment to a new bank account for an established vendor: the classic
    'we changed our bank details' email scam."""
    out = []
    for group in _by_vendor(invoices).values():
        seen: Counter = Counter()
        for inv in group:
            if inv.bank_idx is None:
                continue
            if sum(seen.values()) >= 3 and inv.bank_idx not in seen:
                out.append(Anomaly(inv.id, "bank_account_change", "high",
                                   f"first payment to a new bank account after "
                                   f"{sum(seen.values())} invoices to the usual one(s)"))
            seen[inv.bank_idx] += 1
    return out


def find_shared_bank_accounts(invoices: list[Invoice]) -> list[Anomaly]:
    """One bank account paid out to several vendors: a fake-vendor signal.
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


def find_split_invoices(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    """Several invoices from one vendor within a few days, each under the approval
    threshold but together over it (splitting a bill to dodge sign-off)."""
    limit = cfg.approval_threshold
    flagged: dict[str, str] = {}
    for group in _by_vendor(invoices).values():
        small = [i for i in group if i.amount < limit]
        start = 0
        for end in range(len(small)):
            while (_day(small[end]) - _day(small[start])).days > cfg.split_window_days:
                start += 1
            window = small[start:end + 1]
            total = sum(i.amount for i in window)
            if len(window) >= 2 and total >= limit:
                for inv in window:
                    flagged[inv.id] = (f"{len(window)} invoices within {cfg.split_window_days} days "
                                       f"total {total:,.2f}, over the {limit:,.0f} approval threshold")
    return [Anomaly(inv_id, "split_invoice", "medium", detail) for inv_id, detail in flagged.items()]


def find_below_threshold(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    floor = cfg.approval_threshold * (1 - cfg.below_threshold_pct)
    return [Anomaly(i.id, "below_threshold", "low",
                    f"amount {i.amount:,.2f} is just under the {cfg.approval_threshold:,.0f} approval threshold")
            for i in invoices if floor <= i.amount < cfg.approval_threshold]


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


EXACT_NAME_COMPARE_LIMIT = 2000   # vendors; above this, only names sharing a start or an end are compared
NAME_COMPARE_BUDGET = 4_000_000   # cheap comparisons per run: a ceiling of a few seconds, whatever the data


def _name_pairs(names: list[str], norm: dict[str, str], threshold: float):
    """Pairs of vendor names that could be look-alikes, without comparing every name with
    every other (which takes minutes for a few thousand vendors and would let one large
    upload freeze the server).

    Cheap tests rule most pairs out before the expensive comparison:
    - length: two names can't be 85% alike if one is much longer than the other;
    - letters: an upper bound from the letters they share (quick_ratio).
    Above EXACT_NAME_COMPARE_LIMIT vendors, names are also grouped by their first two and
    last three characters and only compared within a group: a look-alike nearly always
    keeps one end of the name it imitates."""
    by_length = sorted(names, key=lambda n: (len(norm[n]), n))
    budget = NAME_COMPARE_BUDGET

    def window(group: list[str]):
        nonlocal budget
        for i, a in enumerate(group):
            na = norm[a]
            longest = len(na) * (2 - threshold) / threshold
            matcher = SequenceMatcher(None, "", na)
            for b in group[i + 1:]:
                nb = norm[b]
                if len(nb) > longest:
                    break   # sorted by length: every later name is longer still
                budget -= 1
                if budget < 0:   # data built to be slow (or truly enormous): stop, stay responsive
                    return
                matcher.set_seq1(nb)
                if matcher.real_quick_ratio() >= threshold and matcher.quick_ratio() >= threshold:
                    yield (a, b) if a < b else (b, a)

    if len(names) <= EXACT_NAME_COMPARE_LIMIT:
        yield from window(by_length)
        return
    blocks: dict[str, list[str]] = defaultdict(list)
    for name in by_length:
        blocks["^" + norm[name][:2]].append(name)
        blocks["$" + norm[name][-3:]].append(name)
    seen = set()
    for group in blocks.values():
        for pair in window(group):
            if pair not in seen:
                seen.add(pair)
                yield pair


def find_similar_vendor_names(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    """A vendor whose name is almost, but not exactly, another vendor's name
    (look-alike vendors, or typos that split one vendor's history in two)."""
    groups = _by_vendor(invoices)
    norm = {name: _normalize_name(name) for name in groups}
    out = []
    for a, b in sorted(_name_pairs(list(groups), norm, cfg.name_similarity)):
        if SequenceMatcher(None, norm[a], norm[b]).ratio() < cfg.name_similarity:
            continue
        minor, major = (a, b) if len(groups[a]) <= len(groups[b]) else (b, a)
        for inv in groups[minor]:
            out.append(Anomaly(inv.id, "similar_vendor_name", "medium",
                               f"vendor '{minor}' looks like existing vendor '{major}'"))
    return out


def find_new_vendor_large(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    """A large invoice from a vendor with almost no history."""
    if len(invoices) < 10:
        return []
    overall = median(i.amount for i in invoices)
    out = []
    for group in _by_vendor(invoices).values():
        if len(group) > cfg.new_vendor_max_invoices:
            continue
        for inv in group:
            if inv.amount > cfg.new_vendor_multiple * overall:
                out.append(Anomaly(inv.id, "new_vendor_large", "medium",
                                   f"new vendor with a large invoice ({inv.amount:,.2f} vs overall "
                                   f"median {overall:,.2f})"))
    return out


def find_round_amounts(invoices: list[Invoice]) -> list[Anomaly]:
    return [Anomaly(i.id, "round_amount", "low", f"suspiciously round amount {i.amount:,.2f}")
            for i in invoices if i.amount >= 1000 and i.amount % 1000 == 0]


def find_totals_mismatch(invoices: list[Invoice]) -> list[Anomaly]:
    """The invoice's own numbers don't agree (line items vs subtotal, or subtotal + tax vs
    total). Common with honest typos, and with altered invoices where only the total was
    changed. Only invoices read from documents carry this information."""
    return [Anomaly(i.id, "totals_mismatch", "medium", i.totals_issue) for i in invoices if i.totals_issue]


def find_weekend_invoices(invoices: list[Invoice]) -> list[Anomaly]:
    return [Anomaly(i.id, "weekend_invoice", "low", f"dated on a weekend ({i.date})")
            for i in invoices if _day(i).weekday() >= 5]


# ---------------------------------------------------------------- machine learning
def ml_features(invoices: list[Invoice]) -> list[list[float]]:
    """One row per invoice: size, deviation from the vendor's norm, timing and vendor history."""
    groups = _by_vendor(invoices)
    logs = {v: [math.log(i.amount) for i in g] for v, g in groups.items()}
    gap_before: dict[str, float] = {}
    for group in groups.values():
        gaps = [(_day(b) - _day(a)).days for a, b in zip(group, group[1:])]
        typical_gap = median(gaps) if gaps else 0
        gap_before[group[0].id] = typical_gap
        for inv, gap in zip(group[1:], gaps):
            gap_before[inv.id] = gap
    rows = []
    for inv in invoices:
        d = _day(inv)
        z = _robust_log_z(inv.amount, logs[inv.vendor]) if len(logs[inv.vendor]) >= 3 else 0.0
        rows.append([
            math.log1p(inv.amount),
            max(-10.0, min(10.0, z)),
            1.0 if d.weekday() >= 5 else 0.0,
            float(d.day),
            math.log1p(gap_before[inv.id]),
            math.log1p(len(groups[inv.vendor])),
        ])
    return rows


def ml_scores(invoices: list[Invoice]) -> dict[str, float]:
    """Isolation Forest anomaly score per invoice (higher = more unusual).
    Returns {} if scikit-learn isn't installed."""
    try:
        import numpy as np
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return {}
    ordered = sorted(invoices, key=lambda i: i.id)  # stable order => reproducible scores
    x = np.array(ml_features(ordered))
    model = IsolationForest(n_estimators=300, random_state=0).fit(x)
    return {inv.id: float(-s) for inv, s in zip(ordered, model.score_samples(x))}


def find_ml_outliers(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    """Flags invoices whose Isolation Forest score stands far out from the rest.
    Uses a data-driven cut-off (median + k x MAD of the scores) instead of a fixed
    percentage, so a clean dataset isn't forced to produce flags."""
    if len(invoices) < cfg.ml_min_rows:
        return []
    scores = ml_scores(invoices)
    if not scores:
        return []
    values = list(scores.values())
    med = median(values)
    mad = median(abs(v - med) for v in values) or 1e-9
    cutoff = med + cfg.ml_sensitivity * mad
    return [Anomaly(inv_id, "ml_outlier", "medium",
                    f"unusual mix of amount, vendor history and timing "
                    f"(Isolation Forest score {score:.2f}, cut-off {cutoff:.2f})")
            for inv_id, score in scores.items() if score > cutoff]


# ---------------------------------------------------------------- credit notes
CREDIT_MATCH_DAYS = 365   # a credit note cancels an invoice issued up to this long before it


def match_credits(invoices: list[Invoice]) -> dict[str, str]:
    """Which invoice each credit note cancels in full: {credit id: invoice id}.

    A credit note for exactly an invoice's amount, from the same vendor, dated on or after
    it, cancels that invoice. If several invoices fit, the most recent one before the
    credit is taken (a correction usually follows quickly), and each invoice is cancelled
    at most once. Credits for other amounts (a price adjustment, a returned item) match
    nothing: they simply reduce what is owed to the vendor."""
    bills: dict[tuple[str, int], list[Invoice]] = defaultdict(list)
    for inv in invoices:
        if inv.kind == "invoice":
            bills[(inv.vendor, round(inv.amount * 100))].append(inv)
    for group in bills.values():
        group.sort(key=lambda i: (i.date, i.id))
    used: set[str] = set()
    matched: dict[str, str] = {}
    for credit in sorted((i for i in invoices if i.kind == "credit"), key=lambda i: (i.date, i.id)):
        candidates = [b for b in bills.get((credit.vendor, round(credit.amount * 100)), ())
                      if b.id not in used and b.date <= credit.date
                      and (_day(credit) - _day(b)).days <= CREDIT_MATCH_DAYS]
        if candidates:
            bill = candidates[-1]
            used.add(bill.id)
            matched[credit.id] = bill.id
    return matched


def find_credit_issues(invoices: list[Invoice], matched: dict[str, str]) -> list[Anomaly]:
    """Checks that only make sense for credit notes.

    - credited_invoice / matched_credit: an invoice and the credit note that cancels it.
      Both are flagged, as a pair: paying the invoice anyway is money lost, and deducting
      the credit from some other payment as well would short the vendor.
    - unmatched_credit: a credit note from a vendor with nothing billed before it, or for
      more than the vendor has ever billed. Fake credits are used to hide missing money
      or to make an account look settled.
    - duplicate_credit: the same credit recorded twice (it would be deducted twice)."""
    out = []
    for credit_id, bill_id in matched.items():
        out.append(Anomaly(bill_id, "credited_invoice", "high",
                           f"cancelled by credit note {credit_id} (same vendor and amount)"))
        out.append(Anomaly(credit_id, "matched_credit", "medium",
                           f"cancels invoice {bill_id} (same vendor and amount)"))
    billed: dict[str, list[Invoice]] = defaultdict(list)
    for inv in invoices:
        if inv.kind == "invoice":
            billed[inv.vendor].append(inv)
    seen: dict[tuple, str] = {}
    for credit in sorted((i for i in invoices if i.kind == "credit"), key=lambda i: (i.date, i.id)):
        key = (credit.vendor, round(credit.amount * 100), credit.date)
        if key in seen:
            out.append(Anomaly(credit.id, "duplicate_credit", "medium",
                               f"same vendor, amount and date as credit note {seen[key]}"))
        else:
            seen[key] = credit.id
        if credit.id in matched:
            continue
        before = [b for b in billed.get(credit.vendor, ()) if b.date <= credit.date]
        if not before:
            out.append(Anomaly(credit.id, "unmatched_credit", "medium",
                               f"credit note from '{credit.vendor}', which has no invoices on file before it"))
        elif credit.amount > sum(b.amount for b in before) + 0.005:
            out.append(Anomaly(credit.id, "unmatched_credit", "medium",
                               f"credit of {credit.amount:,.2f} is more than everything '{credit.vendor}' "
                               f"has billed ({sum(b.amount for b in before):,.2f})"))
    return out


# ---------------------------------------------------------------- combine
def detect(invoices: list[Invoice], cfg: DetectionConfig = DEFAULT_CONFIG) -> list[Anomaly]:
    # Credit notes are not purchases. The checks that learn what is normal for a vendor
    # (amounts, timing, splits, the ML model) look at invoices only, so a credit can't skew
    # them or be mistaken for a duplicate of the invoice it cancels.
    bills = [i for i in invoices if i.kind == "invoice"]
    matched = match_credits(invoices)
    cancelled = set(matched.values())
    # An invoice that was cancelled and then re-issued correctly is not a duplicate of its
    # replacement: leave cancelled invoices out of the duplicate checks.
    live = [i for i in bills if i.id not in cancelled]
    found = [
        *find_duplicates(live),
        *find_near_duplicates(live, cfg),
        *find_amount_outliers(bills, cfg),
        *find_bank_account_changes(invoices),
        *find_shared_bank_accounts(invoices),
        *find_split_invoices(bills, cfg),
        *find_below_threshold(bills, cfg),
        *find_similar_vendor_names(invoices, cfg),
        *find_new_vendor_large(bills, cfg),
        *find_round_amounts(bills),
        *find_weekend_invoices(bills),
        *find_totals_mismatch(invoices),
        *find_credit_issues(invoices, matched),
    ]
    if cfg.use_ml:
        found += find_ml_outliers(bills, cfg)
    return sorted(found, key=lambda a: (SEVERITY_RANK[a.severity], a.invoice_id, a.rule))


def learned_weights(feedback: dict[str, tuple[int, int]], prior: int = 5) -> dict[str, float]:
    """Adjust each rule's weight using a company's own review decisions.

    feedback is {rule: (confirmed, dismissed)}. A rule's weight is multiplied by
    (confirmed + prior) / (confirmed + dismissed + prior): with no reviews nothing
    changes, and a rule whose flags keep being dismissed fades (to a floor of 0.05,
    so it is never silenced completely). `prior` is how many reviews it takes before
    feedback starts to dominate.
    """
    weights = dict(RULE_WEIGHT)
    for rule, (confirmed, dismissed) in feedback.items():
        if rule in weights:
            factor = (confirmed + prior) / (confirmed + dismissed + prior)
            weights[rule] = round(max(0.05, weights[rule] * factor), 3)
    return weights


def risk_scores(anomalies: list[Anomaly], weights: dict[str, float] | None = None) -> dict[str, int]:
    """Risk 0-100 per invoice. Signals combine as a noisy-OR: independent weak signals
    add up, but no amount of weak signals outranks certainty. Highest first."""
    weights = RULE_WEIGHT if weights is None else weights
    miss: dict[str, float] = defaultdict(lambda: 1.0)
    for a in anomalies:
        weight = weights.get(a.rule, 0.3)
        if a.rule == "amount_outlier" and a.severity == "high":
            weight = min(0.95, weight * 0.75 / RULE_WEIGHT["amount_outlier"])
        miss[a.invoice_id] *= 1 - weight
    scores = {inv_id: round(100 * (1 - m)) for inv_id, m in miss.items()}
    return dict(sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])))


def review_queue(anomalies: list[Anomaly], weights: dict[str, float] | None = None) -> list[dict]:
    """Invoices ranked by risk, each with the reasons it was flagged."""
    reasons: dict[str, list[Anomaly]] = defaultdict(list)
    for a in anomalies:
        reasons[a.invoice_id].append(a)
    return [{"invoice_id": inv_id, "risk": risk,
             "rules": [a.rule for a in reasons[inv_id]],
             "details": [a.detail for a in reasons[inv_id]]}
            for inv_id, risk in risk_scores(anomalies, weights).items()]
