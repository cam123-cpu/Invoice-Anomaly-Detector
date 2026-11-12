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
from collections import defaultdict

from .demo import generate_demo
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
        found = detect(to_invoices(rows), cfg)
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
        f"Precision: {result['precision']:.1%}   Recall: {result['recall']:.1%}   "
        f"Top-k hit rate: {result['top_k_hit_rate']:.1%}",
        "", "Recall by rule:",
        *[f"  {rule:<22}{value:.0%}" for rule, value in result["rule_recall"].items()],
        "", "False flags by rule (on clean invoices, all datasets):",
        *([f"  {rule:<22}{n}" for rule, n in result["false_flags_by_rule"].items()] or ["  none"]),
    ]
    return "\n".join(lines)
