"""Measure the detector on synthetic data with known, injected anomalies.

Reports, averaged over several random datasets:
  recall     share of injected invoices that were flagged at or above the risk cut-off
  precision  share of flagged invoices that were really injected
  rule recall  per rule, how often it fired where it should have
  top-k hit rate  if you review only the k riskiest invoices (k = number injected),
                  what share of them are real

Synthetic scores are an upper bound: real invoices are messier. Re-measure on your own
labelled data before trusting any number here.
"""
from datetime import date, timedelta
from collections import defaultdict

from .demo import generate_demo
from dataclasses import replace

from .demo import DEMO_END
from .detection import DEFAULT_CONFIG, DetectionConfig, Invoice, detect, risk_scores


def to_invoices(rows: list[dict]) -> list[Invoice]:
    # in the real app bank_idx is a keyed hash; the raw string works the same for matching
    return [Invoice(r["id"], r["vendor"], r["amount"], r["date"], r["bank_account"], kind=r.get("kind", "invoice"))
            for r in rows]


def evaluate(seeds=range(10), min_risk: int = 30, cfg: DetectionConfig = DEFAULT_CONFIG) -> dict:
    tp = fp = fn = 0
    topk_hits = topk_total = 0
    rule_hit: dict[str, int] = defaultdict(int)
    rule_total: dict[str, int] = defaultdict(int)
    false_rules: dict[str, int] = defaultdict(int)
    invoices_total = 0

    for seed in seeds:
        rows, injected = generate_demo(seed)
        # "today" is the end of the demo period, so the result doesn't depend on when it runs
        run_cfg = cfg if cfg.today else replace(cfg, today=DEMO_END)
        found = detect(to_invoices(rows), run_cfg)
        risks = risk_scores(found)
        invoices_total += len(rows)

        flagged = {inv_id for inv_id, risk in risks.items() if risk >= min_risk}
        tp += len(flagged & injected.keys())
        fp += len(flagged - injected.keys())
        fn += len(injected.keys() - flagged)

        k = len(injected)
        topk_hits += len(set(list(risks)[:k]) & injected.keys())
        topk_total += k

        fired = defaultdict(set)
        for a in found:
            fired[a.invoice_id].add(a.rule)
            if a.invoice_id not in injected:
                false_rules[a.rule] += 1
        for inv_id, rules in injected.items():
            for rule in rules:
                rule_total[rule] += 1
                rule_hit[rule] += rule in fired[inv_id]

    return {
        "datasets": len(list(seeds)),
        "invoices": invoices_total,
        "min_risk": min_risk,
        "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "top_k_hit_rate": round(topk_hits / topk_total, 3) if topk_total else 1.0,
        "rule_recall": {r: round(rule_hit[r] / n, 3) for r, n in sorted(rule_total.items())},
        "false_flags_by_rule": dict(sorted(false_rules.items())),
    }


def format_report(result: dict) -> str:
    lines = [
        f"Datasets: {result['datasets']}   invoices: {result['invoices']}   "
        f"risk cut-off: {result['min_risk']}",
        f"Precision: {result['precision']:.1%}   Recall: {result['recall']:.1%}"
        + (f"   Top-k hit rate: {result['top_k_hit_rate']:.1%}" if result.get("top_k_hit_rate") is not None else ""),
        "", "Recall by rule:",
        *[f"  {rule:<22}{value:.0%}" for rule, value in result["rule_recall"].items()],
        "", "False flags by rule (on clean invoices, all datasets):",
        *([f"  {rule:<22}{n}" for rule, n in result["false_flags_by_rule"].items()] or ["  none"]),
    ]
    return "\n".join(lines)


def _messy_row(inv_id, vendor, amount, when, rules=None):
    return {"id": inv_id, "vendor": vendor, "amount": amount, "date": when,
            "bank_account": "BANK-" + vendor[:12], "kind": "invoice", "rules": rules or set()}


def evaluate_adversarial(min_risk: int = 30) -> dict:
    """One messy ledger the synthetic generator never builds.

    The ordinary benchmark forces weekdays, avoids round thousands, and plants
    each case with the exact key the matching rule hashes. This ledger does the
    opposite: a retainer that is always a round thousand, a vendor that invoices
    on Saturday, a weekly fixed fee inside the near-duplicate window, the same
    bill twice with float dust, and the same vendor in two capitalizations.
    It does not replace evaluate(). A high score here is not the goal; the
    false flags are the result.
    """
    rows = []
    # Mondays from 2026-01-05, two weeks apart. Round on purpose. Unlabeled.
    for i in range(12):
        day = date(2026, 1, 5) + timedelta(days=14 * i)
        rows.append(_messy_row(f"RC-{8100 + i * 17}", "RoundCo", 5000.00, day.isoformat()))
    # Saturdays from 2026-01-10. Unlabeled.
    for i in range(8):
        day = date(2026, 1, 10) + timedelta(days=14 * i)
        rows.append(_messy_row(f"SC-{4200 + i * 13}", "SatCo", 640.00 + 3 * i, day.isoformat()))
    # Mondays, one week apart, fixed fee inside the 10-day window. Unlabeled.
    for i in range(6):
        day = date(2026, 1, 5) + timedelta(days=7 * i)
        rows.append(_messy_row(f"RT-{3300 + i * 19}", "RetainCo", 1500.00, day.isoformat()))
    dust = 100007 / 100 + 333 / 100
    exact = 100340 / 100
    rows.append(_messy_row("NS-1", "NoiseCo", exact, "2026-03-02"))
    rows.append(_messy_row("NS-2", "NoiseCo", dust, "2026-03-02", {"duplicate"}))
    rows.append(_messy_row("AC-1", "Acme Supplies", 480.00, "2026-01-06"))
    rows.append(_messy_row("AC-2", "ACME  SUPPLIES", 480.00, "2026-01-06", {"duplicate"}))
    rows.append(_messy_row("AC-3", "Acme Suppiles", 495.60, "2026-01-08", {"similar_vendor_name"}))

    expected = {r["id"]: set(r["rules"]) for r in rows}
    found = detect(to_invoices(rows), replace(DEFAULT_CONFIG, today="2026-06-01", use_ml=False))
    risks = risk_scores(found)
    flagged = {inv_id for inv_id, risk in risks.items() if risk >= min_risk}
    labeled = {inv_id for inv_id, rules in expected.items() if rules}
    tp = len(flagged & labeled)
    fp = len(flagged - labeled)
    fn = len(labeled - flagged)
    fired: dict[str, set[str]] = defaultdict(set)
    false_rules: dict[str, int] = defaultdict(int)
    for a in found:
        fired[a.invoice_id].add(a.rule)
        if a.invoice_id not in labeled:
            false_rules[a.rule] += 1
    return {
        "datasets": 1,
        "invoices": len(rows),
        "min_risk": min_risk,
        "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
        "recall": round(tp / (tp + fn), 3) if tp + fn else 1.0,
        "top_k_hit_rate": None,
        "rule_recall": {},
        "false_flags_by_rule": dict(sorted(false_rules.items())),
        "fired": {inv_id: sorted(rules) for inv_id, rules in fired.items()},
    }
