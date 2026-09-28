"""Business policy: the knobs a finance team may tune, with guardrails.

Customisation is allowed only inside safe bounds:
  * numeric tolerances have hard min/max limits
  * some rule outcomes offer a choice (e.g. unknown vendor: review or reject)
  * core controls are LOCKED and cannot be relaxed from the UI:
      - bank account mismatch / invalid IBAN      -> always review
      - exact duplicate (same file or same number) -> always reject
      - totals that don't reconcile                -> always review
      - missing invoice number / date / total      -> always review
Every run stores the policy version it was decided under, so old decisions
stay explainable after the policy changes.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from . import config


class Policy(BaseModel):
    amount_tolerance: Decimal = Field(default=config.AMOUNT_TOLERANCE, ge=0, le=5,
                                      description="Rounding tolerance when comparing amounts")
    po_overrun_pct: Decimal = Field(default=config.PO_OVERRUN_TOLERANCE * 100, ge=0, le=10,
                                    description="How far over a PO the total may go, in %")
    duplicate_window_days: int = Field(default=config.DUPLICATE_WINDOW_DAYS, ge=7, le=90,
                                       description="Look-back window for same-amount resubmissions")
    stale_invoice_days: int = Field(default=config.STALE_INVOICE_DAYS, ge=90, le=1095,
                                    description="Invoices older than this are flagged")
    approval_limit: Decimal = Field(default=Decimal("0"), ge=0, le=10_000_000,
                                    description="Totals above this always need a human (0 = off)")
    po_required_above: Decimal = Field(default=Decimal("0"), ge=0, le=10_000_000,
                                       description="Totals above this must quote a PO (0 = off)")
    date_order: Literal["MDY", "DMY"] = config.DATE_ORDER  # type: ignore[assignment]

    # Choices - only between two safe options each
    unknown_vendor_action: Literal["review", "reject"] = "review"
    near_duplicate_action: Literal["review", "reject"] = "review"
    stale_invoice_action: Literal["note", "review"] = "note"
    receipt_action: Literal["reject", "review"] = "reject"

    @property
    def po_overrun(self) -> Decimal:
        return self.po_overrun_pct / 100


LOCKED_CONTROLS = [
    {"rule": "bank_details", "action": "review", "why": "Changed or invalid bank account - classic payment fraud"},
    {"rule": "duplicate", "action": "reject", "why": "Same file, or same vendor + invoice number, already billed"},
    {"rule": "arithmetic", "action": "review", "why": "Totals that don't add up are never paid automatically"},
    {"rule": "required_fields", "action": "review", "why": "No invoice number, date or total - cannot be booked"},
]
