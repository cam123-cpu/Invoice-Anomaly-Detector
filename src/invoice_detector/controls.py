"""Payment controls: who may release a flagged invoice, and when a second person must agree.

These are the checks an auditor asks about first, kept apart from the web layer so they are
easy to read and to test:

- **Separation of duties.** The person who added an invoice does not release its flags.
  Otherwise one person could enter a fake invoice and wave it through. It applies whenever
  the company has at least two people who can review (a company of one can't separate
  anything; their reviews are still in the activity log).
- **Second approval.** Releasing a flagged invoice for payment (dismissing its flags, or
  confirming them but choosing "pay") at or above the company's approval threshold needs a
  second, different person: not the reviewer, and not whoever added the invoice.
- **Paid.** Only an invoice whose decision is "pay" can be marked paid. A paid invoice
  leaves the payment file.
"""
from __future__ import annotations

REVIEWER_ROLES = ("admin", "accountant")


class ControlError(Exception):
    """A control stops this action. .status is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.message, self.status = message, status


def can_separate(reviewers: int) -> bool:
    return reviewers >= 2


def check_reviewer(added_by: str | None, reviewer: str, reviewers: int) -> None:
    if added_by and added_by == reviewer and can_separate(reviewers):
        raise ControlError("You added this invoice, so someone else must review it (separation of duties).")


def releases_payment(verdict: str, action: str | None) -> bool:
    return verdict == "dismissed" or action == "pay"


def needs_second_approval(amount: float, kind: str, threshold: float, verdict: str, action: str | None,
                          reviewers: int) -> bool:
    """Does releasing this flagged invoice need a second person?"""
    return (kind != "credit" and releases_payment(verdict, action) and abs(amount) >= threshold
            and can_separate(reviewers))


def check_approver(approver: str, reviewer: str | None, added_by: str | None) -> None:
    if approver == reviewer:
        raise ControlError("You reviewed this invoice; the second approval must come from someone else.")
    if added_by and approver == added_by:
        raise ControlError("You added this invoice; the second approval must come from someone else.")
