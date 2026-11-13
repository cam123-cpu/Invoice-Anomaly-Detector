"""What to do about a flagged invoice: a payment decision and concrete next steps.

The system never edits or deletes an invoice. Invoices are the record of what a supplier
sent, and auditors need them unchanged. "Fixing" a problem means deciding what happens to
the payment and doing the right follow-up, so this module turns flags and review
decisions into exactly that. It is pure: no database, no web.

Payment decisions, in order of severity:
  do_not_pay  a confirmed problem that must not be paid (a duplicate)
  hold        a confirmed problem that needs checking before payment
  review      flagged, not yet reviewed: look before paying
  pay         no flags, or flags dismissed as false alarms
"""

ACTIONS = ("pay", "hold", "do_not_pay")  # what a reviewer can choose
DECISIONS = ("do_not_pay", "hold", "review", "pay")
DECISION_TEXT = {"do_not_pay": "Do not pay", "hold": "Hold", "review": "Review first", "pay": "Pay"}

# For each rule: the action suggested when a reviewer confirms it, and what to do next.
RULE_FIX = {
    "duplicate": ("do_not_pay",
                  "Don't pay: this repeats an invoice already on file. If it was paid already, ask the "
                  "vendor for a refund or credit note."),
    "near_duplicate": ("do_not_pay",
                       "Don't pay unless the vendor confirms these are two separate deliveries; ask for "
                       "proof (delivery note or order number)."),
    "bank_account_change": ("hold",
                            "Before paying, call the vendor on a phone number you already had (not one from "
                            "this invoice or email) and confirm the new bank details. Update the vendor record "
                            "only after that."),
    "shared_bank_account": ("hold",
                            "Find out why two vendors are paid into the same account. Check the vendor master "
                            "file for a fake or duplicated vendor before paying either."),
    "amount_outlier": ("hold",
                       "Check the amount with whoever ordered the goods or service. Look for a typo "
                       "(an extra zero), the wrong currency, or a missing approval."),
    "split_invoice": ("hold",
                      "Treat these invoices as one purchase and get sign-off for the combined amount."),
    "similar_vendor_name": ("hold",
                            "Check whether this is a look-alike of a real vendor (possible fraud) or a typo "
                            "that created a second record for the same vendor. Block or merge it."),
    "new_vendor_large": ("hold",
                         "Verify the new vendor before paying: company registration, a signed order or "
                         "contract, and a known contact."),
    "ml_outlier": ("hold",
                   "Read the invoice closely: no single check matched, but its mix of amount, timing and "
                   "vendor history is unusual."),
    "below_threshold": ("hold",
                        "Check whether the amount was set to stay under the approval limit; if so, get the "
                        "approval it should have had."),
    "round_amount": ("hold", "Ask the vendor for an itemised breakdown supporting the round figure."),
    "credited_invoice": ("do_not_pay",
                         "Don't pay this invoice: the vendor cancelled it with a credit note. Confirm the "
                         "credit note is genuine, then mark both as dealt with."),
    "matched_credit": ("do_not_pay",
                       "This credit note cancels one specific invoice. Don't also deduct it from another "
                       "payment to the vendor. Confirm it together with the invoice it cancels."),
    "unmatched_credit": ("hold",
                         "Ask the vendor which invoice this credit relates to before deducting it. A credit "
                         "with nothing behind it can be used to hide a missing payment."),
    "duplicate_credit": ("do_not_pay", "Apply only one of the two identical credit notes."),
    "duplicate_number": ("do_not_pay", "The vendor already sent an invoice with this number. Pay only one: check "
                                       "whether this is a reminder or a corrected copy of the first."),
    "future_date": ("hold", "Check the date with the vendor: an invoice from the future is a typo, or an attempt "
                            "to change when payment falls due."),
    "sequential_numbers": ("hold", "Check that this vendor is a real, independent business (company register, VAT "
                                   "number, address) before paying: it seems to bill nobody but you."),
    "reissued_invoice": ("hold",
                         "This looks like the corrected copy of a cancelled invoice. Pay it once the credit note "
                         "is confirmed with the vendor (by phone, not by replying to the email): if the original "
                         "was already paid and the credit is fake, paying this copy pays twice."),
    "totals_mismatch": ("hold",
                        "Recalculate the invoice. Pay only the correct amount, and ask the vendor for a corrected "
                        "invoice; if the total looks altered, confirm it with the vendor by phone."),
    "weekend_invoice": ("hold",
                        "Confirm the delivery or work date. A weekend date can mean a backdated or invented "
                        "invoice."),
}

SEVERITY = {"do_not_pay": 0, "hold": 1, "review": 2, "pay": 3}


def suggested_action(rules: list[str]) -> str:
    """The action to apply when a reviewer confirms an invoice flagged by these rules:
    the strictest action any of them suggests."""
    actions = [RULE_FIX.get(r, ("hold", ""))[0] for r in rules] or ["hold"]
    return min(actions, key=SEVERITY.__getitem__)


def fix_steps(rules: list[str]) -> list[str]:
    """Plain-English next steps, one per distinct rule, strictest first."""
    seen, steps = set(), []
    for rule in sorted(set(rules), key=lambda r: (SEVERITY[RULE_FIX.get(r, ("hold", ""))[0]], r)):
        text = RULE_FIX.get(rule, ("hold", "Look at this invoice before paying."))[1]
        if text not in seen:
            seen.add(text)
            steps.append(text)
    return steps


def payment_decision(item: dict | None) -> str:
    """Decision for one invoice, given its review-queue item (None if it has no flags).

    The item may carry 'status' (open/confirmed/dismissed), 'action' (the reviewer's
    choice, if any) and 'rules'."""
    if item is None:
        return "pay"
    status = item.get("status", "open")
    if status == "dismissed":
        decision = "pay"
    elif status == "confirmed":
        action = item.get("action") or suggested_action(item.get("rules", []))
        decision = action if action in ACTIONS else "hold"
    else:
        return "review"
    if decision == "pay" and item.get("needs_approval") and not item.get("approved_by"):
        return "review"        # released by one person; a second must agree (controls.py)
    return decision
