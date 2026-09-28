"""Data shapes that flow through the pipeline.

RawExtraction  -> what the LLM read off the page (strings, exactly as printed)
Invoice        -> cleaned, typed values produced by the transform step
Finding        -> the result of one validation rule
Decision       -> the final outcome with its reasons
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class RawLine(BaseModel):
    description: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    amount: str | None = None


DocType = Literal["invoice", "receipt", "credit_note", "quote", "other"]


class RawExtraction(BaseModel):
    document_type: DocType = "invoice"
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    vendor_iban: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    currency: str | None = None
    po_number: str | None = None
    line_items: list[RawLine] = Field(default_factory=list)
    subtotal: str | None = None
    tax: str | None = None
    total: str | None = None
    tax_rate: str | None = None       # printed rate, e.g. "18%"
    amount_paid: str | None = None    # "Amount paid" / payments received
    balance_due: str | None = None    # "Balance due" / "Amount due"
    tax_inclusive_hint: bool | None = None  # page says e.g. "prices include VAT"
    unclear_fields: list[str] = Field(default_factory=list)  # model was unsure


class Line(BaseModel):
    description: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None


class Invoice(BaseModel):
    document_type: DocType = "invoice"
    vendor_name: str | None
    vendor_tax_id: str | None
    vendor_iban: str | None
    invoice_number: str | None
    invoice_number_norm: str | None
    invoice_date: date | None
    due_date: date | None
    currency: str | None
    po_number: str | None
    lines: list[Line]
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    tax_rate: Decimal | None = None   # percent
    amount_paid: Decimal | None = None
    balance_due: Decimal | None = None
    tax_inclusive_hint: bool | None
    unclear_fields: list[str]


Outcome = Literal["pass", "warn", "fail", "skip"]
# What a finding asks the decision step to do. Ordered by severity.
Action = Literal["none", "note", "review", "reject"]
ACTION_RANK = {"none": 0, "note": 1, "review": 2, "reject": 3}


class Finding(BaseModel):
    rule: str
    outcome: Outcome
    action: Action
    message: str
    evidence: dict = Field(default_factory=dict)


class Decision(BaseModel):
    outcome: Literal["APPROVE", "REVIEW", "REJECT"]
    summary: str
    reasons: list[str]
