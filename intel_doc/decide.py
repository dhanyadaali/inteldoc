"""DECIDE: the strictest action requested by any rule wins. No model involved."""
from .models import ACTION_RANK, Decision, Finding

OUTCOME = {"none": "APPROVE", "note": "APPROVE", "review": "REVIEW", "reject": "REJECT"}


def decide(findings: list[Finding]) -> Decision:
    worst = max((f.action for f in findings), key=ACTION_RANK.__getitem__, default="none")
    outcome = OUTCOME[worst]
    blocking = [f for f in findings if f.action == worst and worst in ("review", "reject")]
    notes = [f for f in findings if f.action == "note"]

    if outcome == "REJECT":
        summary = "Rejected: " + "; ".join(f.message for f in blocking)
    elif outcome == "REVIEW":
        summary = f"Needs human review ({len(blocking)} issue{'s' * (len(blocking) != 1)}): " + \
                  "; ".join(f.rule for f in blocking)
    else:
        passed = sum(f.outcome == "pass" for f in findings)
        summary = f"Approved: {passed} checks passed" + (f", {len(notes)} note(s)" if notes else "")

    reasons = [f"[{f.action.upper()}] {f.rule}: {f.message}" for f in blocking + notes]
    return Decision(outcome=outcome, summary=summary, reasons=reasons)
