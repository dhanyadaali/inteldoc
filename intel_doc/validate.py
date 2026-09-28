"""VALIDATE: deterministic rules. Each returns a Finding with its working shown.

Every rule states what it should do if it fails:
  note   -> keep going, but mention it
  review -> a human must look before payment
  reject -> never pay this
Choices a finance team may tune come from Policy; locked controls ignore it.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from rapidfuzz import fuzz

from .models import Finding, Invoice
from .policy import Policy
from .store import Store, Vendor
from .transform import iban_valid, norm_id

CRITICAL_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total")
TAX_RATE_TOLERANCE_PP = Decimal("0.5")  # printed vs computed rate, percentage points


def _d(x: Decimal | None) -> str | None:
    return None if x is None else f"{x:,.2f}"


def _q(x: Decimal) -> str:
    return f"{x.normalize():f}" if x == x.to_integral() else f"{x:f}"


# ---------------------------------------------------------------- document
def check_document_type(inv: Invoice, p: Policy) -> Finding:
    """A receipt or quote is not a demand for payment. Paying a receipt pays twice."""
    t = inv.document_type
    paid_in_full = (inv.amount_paid is not None and inv.total is not None and inv.amount_paid >= inv.total
                    and (inv.balance_due is None or inv.balance_due == 0))
    ev = {"document_type": t, "amount_paid": _d(inv.amount_paid), "balance_due": _d(inv.balance_due)}
    if t == "receipt" or (t == "invoice" and paid_in_full):
        return Finding(rule="document_type", outcome="fail", action=p.receipt_action, evidence=ev,
                       message=f"Already paid - this is a {'receipt' if t == 'receipt' else 'paid invoice'} "
                               f"({_d(inv.amount_paid) or _d(inv.total)} paid). Nothing to pay; file for records")
    if t == "quote":
        return Finding(rule="document_type", outcome="fail", action="reject", evidence=ev,
                       message="This is a quote / pro-forma, not an invoice - no payment is due yet")
    if t == "credit_note":
        return Finding(rule="document_type", outcome="fail", action="review", evidence=ev,
                       message="Credit note - must be offset against an open invoice, not paid")
    if t == "other":
        return Finding(rule="document_type", outcome="fail", action="review", evidence=ev,
                       message="Document does not look like an invoice")
    due = f"; {_d(inv.balance_due)} due" if inv.balance_due is not None else ""
    return Finding(rule="document_type", outcome="pass", action="none", evidence=ev,
                   message=f"Invoice requesting payment{due}")


def check_required_fields(inv: Invoice) -> Finding:
    missing = [f for f in CRITICAL_FIELDS if getattr(inv, f) in (None, "")]
    if missing:
        return Finding(rule="required_fields", outcome="fail", action="review",
                       message=f"Missing critical fields: {', '.join(missing)} - cannot be booked",
                       evidence={"missing": missing})
    return Finding(rule="required_fields", outcome="pass", action="none",
                   message=f"No. {inv.invoice_number} · dated {inv.invoice_date} · total {_d(inv.total)} "
                           f"· from {inv.vendor_name}")


def check_extraction_confidence(inv: Invoice) -> Finding:
    """The model flags fields it could not read. If money or identity is among them, don't guess."""
    shaky = [f for f in inv.unclear_fields if any(f.startswith(c) for c in
             (*CRITICAL_FIELDS, "vendor_iban", "subtotal", "tax", "line_items", "po_number"))]
    if shaky:
        return Finding(rule="extraction_confidence", outcome="fail", action="review",
                       message=f"Model could not read confidently: {', '.join(shaky)}", evidence={"fields": shaky})
    if inv.unclear_fields:
        return Finding(rule="extraction_confidence", outcome="warn", action="note",
                       message=f"Unclear non-critical fields: {', '.join(inv.unclear_fields)}")
    return Finding(rule="extraction_confidence", outcome="pass", action="none",
                   message="Model flagged no fields as unclear")


# ---------------------------------------------------------------- arithmetic
def check_arithmetic(inv: Invoice, p: Policy) -> Finding:
    """Recompute lines -> subtotal -> tax -> total, and show the working.

    Edge case: tax-INCLUSIVE invoices. Lines already contain the tax, so
    sum(lines) == total, not subtotal. A naive check rejects these. We try the
    exclusive reading first, then the inclusive one, and record which one fits.
    If neither fits, it's a real error.
    """
    tol = p.amount_tolerance
    if inv.total is None:
        return Finding(rule="arithmetic", outcome="skip", action="none", message="No total to check")

    def close(a, b):
        return a is not None and b is not None and abs(a - b) <= tol

    # 1. each line: qty x price == amount
    line_work, bad_lines = [], []
    for i, l in enumerate(inv.lines, 1):
        if None not in (l.quantity, l.unit_price, l.amount):
            expected = (l.quantity * l.unit_price).quantize(Decimal("0.01"))
            line_work.append(f"{_q(l.quantity)} × {_d(l.unit_price)} = {_d(expected)}")
            if abs(expected - l.amount) > tol:
                bad_lines.append({"line": i, "computed": f"{_q(l.quantity)} × {_d(l.unit_price)} = {_d(expected)}",
                                  "printed": _d(l.amount)})
    if bad_lines:
        b = bad_lines[0]
        return Finding(rule="arithmetic", outcome="fail", action="review", evidence={"lines": bad_lines},
                       message=f"Line {b['line']}: {b['computed']} but invoice prints {b['printed']}"
                               + (f" (+{len(bad_lines) - 1} more)" if len(bad_lines) > 1 else ""))

    line_sum = sum((l.amount for l in inv.lines if l.amount is not None), Decimal("0"))
    has_lines = any(l.amount is not None for l in inv.lines)
    tax = inv.tax or Decimal("0")
    base = inv.subtotal if inv.subtotal is not None else line_sum
    ev = {"lines": line_work, "line_sum": _d(line_sum), "subtotal": _d(inv.subtotal), "tax": _d(tax),
          "total": _d(inv.total)}

    exclusive_ok = close(base + tax, inv.total) and (not has_lines or close(line_sum, base))
    inclusive_ok = has_lines and close(line_sum, inv.total) and (
        inv.subtotal is None or close(inv.total - tax, inv.subtotal))

    if (inv.tax_inclusive_hint is True and inclusive_ok) or (not exclusive_ok and inclusive_ok):
        mode = "tax_inclusive" if tax > 0 else "no_tax"
        work = f"lines {_d(line_sum)} = total {_d(inv.total)} (tax {_d(tax)} included)"
        if inv.tax_inclusive_hint is not True and tax > 0:
            return Finding(rule="arithmetic", outcome="warn", action="note", evidence={**ev, "mode": mode},
                           message=f"{work} - only reconciles as tax-inclusive, though the page doesn't say so")
        finding_mode, finding_work = mode, work
    elif exclusive_ok:
        finding_mode = "tax_exclusive"
        finding_work = f"lines {_d(line_sum)} = subtotal {_d(base)} · {_d(base)} + tax {_d(tax)} = {_d(inv.total)}"
    else:
        diff = inv.total - (base + tax)
        if abs(diff) <= tol and has_lines:
            gap = base - line_sum
            return Finding(rule="arithmetic", outcome="fail", action="review", evidence={**ev, "difference": _d(gap)},
                           message=f"Line items sum to {_d(line_sum)} but subtotal is {_d(base)} "
                                   f"(gap {_d(gap)}) - a line may be missing or misread")
        return Finding(rule="arithmetic", outcome="fail", action="review", evidence={**ev, "difference": _d(diff)},
                       message=f"{_d(base)} + tax {_d(tax)} = {_d(base + tax)}, but invoice total is "
                               f"{_d(inv.total)} (off by {_d(diff)}), under either tax reading")

    # 2. tax rate: printed rate vs rate implied by the numbers
    net = inv.total - tax if finding_mode == "tax_inclusive" else base
    if tax > 0 and net > 0:
        computed = (tax / net * 100).quantize(Decimal("0.1"))
        ev["tax_rate_computed"] = f"{computed}%"
        finding_work += f" · tax is {computed}% of net"
        if inv.tax_rate is not None:
            ev["tax_rate_printed"] = f"{_q(inv.tax_rate)}%"
            if abs(computed - inv.tax_rate) > TAX_RATE_TOLERANCE_PP:
                return Finding(rule="arithmetic", outcome="fail", action="review", evidence={**ev, "mode": finding_mode},
                               message=f"Tax {_d(tax)} is {computed}% of net {_d(net)}, "
                                       f"but the invoice prints {_q(inv.tax_rate)}%")
            finding_work += f" (printed {_q(inv.tax_rate)}%)"
    return Finding(rule="arithmetic", outcome="pass", action="none", evidence={**ev, "mode": finding_mode},
                   message=finding_work)


# ---------------------------------------------------------------- dates
def check_dates(inv: Invoice, today: date, p: Policy) -> Finding:
    d = inv.invoice_date
    if d is None:
        return Finding(rule="dates", outcome="skip", action="none", message="No invoice date to check")
    if d > today:
        return Finding(rule="dates", outcome="fail", action="review", message=f"Invoice dated in the future ({d})")
    if inv.due_date and inv.due_date < d:
        return Finding(rule="dates", outcome="fail", action="review",
                       message=f"Due date {inv.due_date} is before invoice date {d}")
    age = (today - d).days
    due = f", due {inv.due_date}" if inv.due_date else ""
    if age > p.stale_invoice_days:
        return Finding(rule="dates", outcome="warn", action=p.stale_invoice_action, evidence={"age_days": age},
                       message=f"Invoice is {age} days old (policy flags > {p.stale_invoice_days})")
    return Finding(rule="dates", outcome="pass", action="none", evidence={"age_days": age},
                   message=f"Dated {d} ({age} days ago){due}")


# ---------------------------------------------------------------- master data
def match_vendor(inv: Invoice, store: Store, p: Policy) -> tuple[Vendor | None, Finding]:
    """Tax id is authoritative. Name matching is only a fallback."""
    if inv.vendor_tax_id:
        v = store.vendor_by_tax_id(inv.vendor_tax_id)
        if v:
            return v, Finding(rule="vendor_match", outcome="pass", action="none",
                              message=f"Approved vendor '{v.name}' - tax id {inv.vendor_tax_id} on file",
                              evidence={"vendor_id": v.id})
    if inv.vendor_name:
        best, score = None, 0.0
        for v in store.all_vendors():
            s = fuzz.token_sort_ratio(inv.vendor_name.lower(), v.name.lower())
            if s > score:
                best, score = v, s
        if best and score >= 90:
            if inv.vendor_tax_id and best.tax_id and norm_id(inv.vendor_tax_id) != norm_id(best.tax_id):
                return best, Finding(rule="vendor_match", outcome="fail", action="review",
                                     message=f"Name matches '{best.name}' but tax id {inv.vendor_tax_id} "
                                             f"differs from {best.tax_id} on file",
                                     evidence={"invoice_tax_id": inv.vendor_tax_id, "master_tax_id": best.tax_id})
            return best, Finding(rule="vendor_match", outcome="pass", action="none",
                                 message=f"Approved vendor '{best.name}' - name match {score:.0f}%",
                                 evidence={"vendor_id": best.id, "score": round(score)})
    return None, Finding(rule="vendor_match", outcome="fail", action=p.unknown_vendor_action,
                         message=f"'{inv.vendor_name}' is not an approved vendor - onboard it before paying",
                         evidence={"name": inv.vendor_name, "tax_id": inv.vendor_tax_id,
                                   "iban": inv.vendor_iban, "currency": inv.currency})


def check_bank_details(inv: Invoice, vendor: Vendor | None) -> Finding:
    """LOCKED. Edge case: bank-account swap (business email compromise).

    Everything else on the invoice can be perfect. If the IBAN differs from the
    one on file, payment must stop until the change is confirmed out-of-band.
    """
    if inv.vendor_iban and not iban_valid(inv.vendor_iban):
        return Finding(rule="bank_details", outcome="fail", action="review",
                       message="IBAN on invoice fails its checksum - misread or altered; do not pay to it",
                       evidence={"invoice_iban": _mask(inv.vendor_iban)})
    if vendor is None:
        return Finding(rule="bank_details", outcome="skip", action="none", message="No approved vendor to compare to")
    if not inv.vendor_iban:
        return Finding(rule="bank_details", outcome="pass", action="none",
                       message="No bank details on invoice - pay to the account on file")
    if not vendor.iban:
        return Finding(rule="bank_details", outcome="fail", action="review",
                       message="Invoice gives bank details but none are verified on file for this vendor",
                       evidence={"invoice_iban": _mask(inv.vendor_iban)})
    if norm_id(inv.vendor_iban) != norm_id(vendor.iban):
        return Finding(rule="bank_details", outcome="fail", action="review",
                       message=f"Bank account {_mask(inv.vendor_iban)} differs from {_mask(vendor.iban)} on file - "
                               "possible payment fraud. Confirm using contact details on file, not the invoice.",
                       evidence={"invoice_iban": _mask(inv.vendor_iban), "master_iban": _mask(vendor.iban)})
    return Finding(rule="bank_details", outcome="pass", action="none",
                   message=f"IBAN {_mask(inv.vendor_iban)} valid and matches the account on file")


def _mask(iban: str) -> str:
    iban = norm_id(iban)
    return f"{iban[:4]}…{iban[-4:]}" if len(iban) > 8 else iban


def check_currency(inv: Invoice, vendor: Vendor | None) -> Finding:
    if vendor is None or inv.currency is None:
        return Finding(rule="currency", outcome="skip", action="none", message="Currency or vendor unknown")
    if inv.currency != vendor.currency:
        return Finding(rule="currency", outcome="fail", action="review",
                       message=f"Invoice in {inv.currency}, but {vendor.name} is set up to bill in {vendor.currency}")
    return Finding(rule="currency", outcome="pass", action="none",
                   message=f"{inv.currency} - matches the vendor's billing currency")


# ---------------------------------------------------------------- history
def check_duplicates(inv: Invoice, vendor: Vendor | None, sha256: str, store: Store, p: Policy) -> Finding:
    """Edge case: resubmitted invoices.

    LOCKED: exact file, or same vendor + same invoice number after normalisation
    ('INV-0042' == 'inv 0042') -> reject.
    Tunable: different number but same vendor, same total, close dates ->
    likely a resubmission under a new number -> review (or reject).
    """
    prior = store.invoice_by_sha(sha256)
    if prior:
        return Finding(rule="duplicate", outcome="fail", action="reject",
                       message=f"Identical file already processed as invoice #{prior['id']}",
                       evidence={"prior_invoice_id": prior["id"]})
    if vendor is None:
        return Finding(rule="duplicate", outcome="skip", action="none", message="Unknown vendor; no history to check")
    if inv.invoice_number_norm:
        prior = store.invoice_by_number(vendor.id, inv.invoice_number_norm)
        if prior:
            return Finding(rule="duplicate", outcome="fail", action="reject",
                           message=f"Invoice number '{inv.invoice_number}' already billed by this vendor "
                                   f"(as '{prior['invoice_number']}', #{prior['id']}, {prior['decision']})",
                           evidence={"prior_invoice_id": prior["id"], "normalised": inv.invoice_number_norm})
    if inv.total is not None and inv.invoice_date:
        similar = store.similar_invoices(vendor.id, inv.total, inv.invoice_date, p.duplicate_window_days)
        if similar:
            s = similar[0]
            return Finding(rule="duplicate", outcome="fail", action=p.near_duplicate_action,
                           message=f"Same vendor and amount ({_d(inv.total)}) as invoice '{s['invoice_number']}' "
                                   f"(#{s['id']}) dated {s['invoice_date']} - possible resubmission under a new number",
                           evidence={"similar": [{k: str(v) for k, v in x.items()} for x in similar]})
    n = store.vendor_invoice_count(vendor.id)
    return Finding(rule="duplicate", outcome="pass", action="none",
                   message=f"Checked {n} earlier invoice(s) from this vendor: no same file, number "
                           f"({inv.invoice_number_norm}) or same amount within {p.duplicate_window_days} days")


def check_purchase_order(inv: Invoice, vendor: Vendor | None, store: Store, p: Policy) -> Finding:
    """Edge case: cumulative PO overrun.

    Each partial invoice can be under the PO on its own, yet together they
    exceed it. We check against what has already been billed (approved or
    pending review) on this PO, not just this invoice.
    No PO quoted: suggest an open PO of the same vendor that would fit
    (implied reference); policy may require a PO above an amount.
    """
    if not inv.po_number:
        return _no_po(inv, vendor, store, p)
    po = store.po(inv.po_number)
    if po is None:
        return Finding(rule="purchase_order", outcome="fail", action="review",
                       message=f"PO {inv.po_number} does not exist")
    if vendor and po.vendor_id != vendor.id:
        return Finding(rule="purchase_order", outcome="fail", action="review",
                       message=f"PO {po.po_number} belongs to a different vendor")
    if inv.total is None:
        return Finding(rule="purchase_order", outcome="skip", action="none", message="No total to check against PO")
    billed = store.po_billed(po.po_number)
    after = billed + inv.total
    limit = po.amount * (1 + p.po_overrun)
    ev = {"po_amount": _d(po.amount), "already_billed": _d(billed), "this_invoice": _d(inv.total),
          "after_this": _d(after), "limit_with_tolerance": _d(limit)}
    work = f"{_d(billed)} billed + {_d(inv.total)} this invoice = {_d(after)} of {_d(po.amount)}"
    if after > limit:
        return Finding(rule="purchase_order", outcome="fail", action="review", evidence=ev,
                       message=f"PO {po.po_number} overrun: {work} (limit {_d(limit)} incl. {_q(p.po_overrun_pct)}%)")
    return Finding(rule="purchase_order", outcome="pass", action="none", evidence=ev,
                   message=f"Within PO {po.po_number}: {work}")


def _no_po(inv: Invoice, vendor: Vendor | None, store: Store, p: Policy) -> Finding:
    fits = []
    if vendor and inv.total is not None:
        for po in store.open_pos(vendor.id):
            remaining = po["amount"] - po["billed"]
            if remaining >= inv.total:
                fits.append(f"{po['po_number']} ({_d(remaining)} left)")
    hint = f"; would fit open {', '.join(fits)}" if fits else ""
    if p.po_required_above and inv.total is not None and inv.total > p.po_required_above:
        return Finding(rule="purchase_order", outcome="fail", action="review", evidence={"candidates": fits},
                       message=f"No PO quoted, and policy requires one above {_d(p.po_required_above)}{hint}")
    if fits:
        return Finding(rule="purchase_order", outcome="warn", action="note", evidence={"candidates": fits},
                       message=f"No PO quoted{hint} - AP may link it")
    return Finding(rule="purchase_order", outcome="skip", action="none", message="No PO quoted - non-PO spend")


def check_approval_limit(inv: Invoice, p: Policy) -> Finding:
    if not p.approval_limit:
        return Finding(rule="approval_limit", outcome="skip", action="none", message="No approval limit set")
    if inv.total is not None and inv.total > p.approval_limit:
        return Finding(rule="approval_limit", outcome="fail", action="review",
                       message=f"Total {_d(inv.total)} is above the auto-approval limit {_d(p.approval_limit)}")
    return Finding(rule="approval_limit", outcome="pass", action="none",
                   message=f"Total {_d(inv.total)} within auto-approval limit {_d(p.approval_limit)}")


def run_all(inv: Invoice, sha256: str, store: Store, today: date | None = None,
            policy: Policy | None = None) -> tuple[Vendor | None, list[Finding]]:
    p = policy or Policy()
    today = today or date.today()
    vendor, vendor_finding = match_vendor(inv, store, p)
    findings = [
        check_document_type(inv, p),
        check_required_fields(inv),
        check_extraction_confidence(inv),
        check_arithmetic(inv, p),
        check_dates(inv, today, p),
        vendor_finding,
        check_bank_details(inv, vendor),
        check_currency(inv, vendor),
        check_duplicates(inv, vendor, sha256, store, p),
        check_purchase_order(inv, vendor, store, p),
        check_approval_limit(inv, p),
    ]
    return vendor, findings
