"""End-to-end logic tests with no LLM and no database.

Each generated scenario is converted to the RawExtraction a perfect reader would
produce (strings exactly as printed), then pushed through transform -> validate
-> decide -> load against an in-memory store, IN ORDER, because several edge
cases depend on history.
"""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_invoices import BRIGHTLINE, NORTHWIND, PURCHASE_ORDERS, SCENARIOS, money  # noqa: E402
from intel_doc.models import RawExtraction, RawLine  # noqa: E402
from intel_doc.pipeline import process_raw  # noqa: E402
from intel_doc.store import MemoryStore, PurchaseOrder, Vendor  # noqa: E402
from intel_doc.transform import parse_money  # noqa: E402

TODAY = date(2026, 9, 27)


def as_printed(s: dict) -> RawExtraction:
    cur = s["vendor"]["currency"]
    return RawExtraction(
        vendor_name=s["vendor"]["name"], vendor_tax_id=s["vendor"]["tax_id"], vendor_iban=s["iban"],
        invoice_number=s["number"], invoice_date=s["date"], due_date=s["due"], po_number=s["po"],
        line_items=[RawLine(description=d, quantity=f"{q:g}", unit_price=money(p, cur), amount=money(a, cur))
                    for (d, q, p), a in zip(s["lines"], s["line_amounts"])],
        subtotal=money(s["subtotal"], cur), tax=money(s["tax"], cur), total=money(s["total"], cur),
        tax_inclusive_hint=True if s["inclusive"] else None,
    )


def fresh_store() -> MemoryStore:
    vendors = [Vendor(i, v["name"], v["tax_id"], v["iban"], v["currency"]) for i, v in enumerate((NORTHWIND, BRIGHTLINE), 1)]
    pos = [PurchaseOrder(p["po_number"], 1, p["currency"], Decimal(p["amount"])) for p in PURCHASE_ORDERS]
    return MemoryStore(vendors, pos)


@pytest.fixture(scope="module")
def results():
    store = fresh_store()
    return {s["file"]: process_raw(as_printed(s), f"sha-{s['file']}", s["file"], store, today=TODAY)
            for s in SCENARIOS}


@pytest.mark.parametrize("s", SCENARIOS, ids=[s["file"] for s in SCENARIOS])
def test_expected_decision(results, s):
    r = results[s["file"]]
    assert r.decision.outcome == s["expected"], r.decision.reasons


def rule(result, name):
    return next(f for f in result.findings if f.rule == name)


def test_po_overrun_is_about_history(results):
    f = rule(results["04_po_partial_3_overrun.pdf"], "purchase_order")
    assert f.evidence["already_billed"] == "8,500.00" and f.evidence["this_invoice"] == "2,400.00"


def test_reformatted_duplicate_rejected_by_normalised_number(results):
    f = rule(results["05_duplicate_reformatted.pdf"], "duplicate")
    assert f.action == "reject" and "INV-2026-0101" in f.message


def test_resubmission_points_at_original(results):
    f = rule(results["06_resubmitted_new_number.pdf"], "duplicate")
    assert f.action == "review" and "INV-2026-0101" in f.message


def test_bank_swap_is_the_only_problem(results):
    r = results["07_bank_details_changed.pdf"]
    blocking = [f.rule for f in r.findings if f.action in ("review", "reject")]
    assert blocking == ["bank_details"]


def test_vat_inclusive_reconciled_in_inclusive_mode(results):
    assert rule(results["08_vat_inclusive.pdf"], "arithmetic").evidence["mode"] == "tax_inclusive"


def test_math_error_still_caught(results):
    f = rule(results["09_math_error.pdf"], "arithmetic")
    assert f.outcome == "fail" and f.evidence["difference"] == "100.00"


def test_rejected_review_frees_po_budget():
    """If a human rejects the overrun invoice, it must stop consuming PO budget."""
    store = fresh_store()
    for s in SCENARIOS[1:4]:
        process_raw(as_printed(s), s["file"], s["file"], store, today=TODAY)
    assert store.po_billed("PO-7781") == Decimal("10900.00")
    store.invoices[-1]["resolution"] = "REJECT"
    assert store.po_billed("PO-7781") == Decimal("8500.00")


def test_exact_same_file_rejected():
    store = fresh_store()
    s = SCENARIOS[0]
    process_raw(as_printed(s), "same-sha", s["file"], store, today=TODAY)
    assert process_raw(as_printed(s), "same-sha", s["file"], store, today=TODAY).decision.outcome == "REJECT"


@pytest.mark.parametrize("text,value", [
    ("$1,757.98", "1757.98"), ("1.680,67 EUR", "1680.67"), ("$ 889,20", "889.20"),
    ("12,50", "12.50"), ("1,234,567", "1234567"), ("(45.00)", "-45.00"), ("€ 1 234,56", "1234.56"),
])
def test_parse_money(text, value):
    assert parse_money(text) == Decimal(value)


def test_ambiguous_date_is_noted():
    from intel_doc.transform import parse_date
    notes = []
    assert parse_date("03/04/2026", notes, "invoice_date") == date(2026, 3, 4)
    assert "ambiguous" in notes[0]
    assert parse_date("15.09.2026", [], "x") == date(2026, 9, 15)


def test_currency_detected_for_every_scenario(results):
    for s in SCENARIOS:
        assert results[s["file"]].invoice.currency == s["vendor"]["currency"], s["file"]


def test_iban_checksum():
    from intel_doc.transform import iban_valid
    assert iban_valid("GB33 BUKB 2020 1555 5555 55")
    assert iban_valid("DE89370400440532013000")
    assert not iban_valid("GB31LZX520242755934691")      # OCR-style misread: S -> 5
    assert not iban_valid("GB08KXQT197119711661357354")  # duplicated digits


def test_misread_iban_goes_to_review():
    s = SCENARIOS[0]
    raw = as_printed(s)
    raw.vendor_iban = "GB29NWBK60161331926818"  # last digit wrong
    r = process_raw(raw, "sha-misread", s["file"], fresh_store(), today=TODAY)
    f = rule(r, "bank_details")
    assert r.decision.outcome == "REVIEW" and "checksum" in f.message


# ---------------------------------------------------------------- documents, tax, policy
from intel_doc.policy import Policy  # noqa: E402


def _receipt(**kw) -> RawExtraction:
    """Shaped like the real OpenAI receipt a user uploaded."""
    base = dict(document_type="receipt", vendor_name="OpenAI OpCo, LLC", vendor_tax_id="0925USA29095OSD",
                invoice_number="1SUOVEA8-0002", invoice_date="May 29, 2026", currency="USD",
                line_items=[RawLine(description="OpenAI API usage credit", quantity="1", unit_price="$25.00",
                                    amount="$25.00")],
                subtotal="$25.00", tax="$4.50", tax_rate="18%", total="$29.50", amount_paid="$29.50")
    return RawExtraction(**{**base, **kw})


def test_receipt_is_never_paid():
    r = process_raw(_receipt(), "sha-r", "receipt.pdf", fresh_store(), today=TODAY)
    f = rule(r, "document_type")
    assert r.decision.outcome == "REJECT" and "Already paid" in f.message


def test_receipt_action_is_customisable_within_bounds():
    r = process_raw(_receipt(), "sha-r2", "receipt.pdf", fresh_store(), today=TODAY,
                    policy=Policy(receipt_action="review"))
    assert rule(r, "document_type").action == "review"
    with pytest.raises(Exception):
        Policy(receipt_action="none")          # cannot be switched off
    with pytest.raises(Exception):
        Policy(amount_tolerance="50")           # outside guardrail


def test_arithmetic_shows_its_working():
    r = process_raw(_receipt(document_type="invoice", amount_paid=None), "sha-w", "r.pdf", fresh_store(), today=TODAY)
    msg = rule(r, "arithmetic").message
    assert "1 × 25.00 = 25.00" not in msg  # line working lives in evidence
    assert "25.00 + tax 4.50 = 29.50" in msg and "18.0% of net" in msg and "printed 18%" in msg


def test_wrong_printed_tax_rate_goes_to_review():
    r = process_raw(_receipt(document_type="invoice", amount_paid=None, tax_rate="20%"), "sha-t", "r.pdf",
                    fresh_store(), today=TODAY)
    f = rule(r, "arithmetic")
    assert f.outcome == "fail" and "prints 20%" in f.message


def test_paid_invoice_counts_as_receipt():
    r = process_raw(_receipt(document_type="invoice", balance_due="$0.00"), "sha-p", "r.pdf", fresh_store(),
                    today=TODAY)
    assert rule(r, "document_type").outcome == "fail"


def test_implied_po_hint_and_po_required_policy():
    s = SCENARIOS[0]  # no PO quoted, Northwind has open PO-7781
    r = process_raw(as_printed(s), "sha-i", s["file"], fresh_store(), today=TODAY)
    f = rule(r, "purchase_order")
    assert f.action == "note" and "PO-7781" in f.message and r.decision.outcome == "APPROVE"
    r2 = process_raw(as_printed(s), "sha-i2", s["file"], fresh_store(), today=TODAY,
                     policy=Policy(po_required_above="1000"))
    assert r2.decision.outcome == "REVIEW" and "requires one above" in rule(r2, "purchase_order").message


def test_approval_limit():
    s = SCENARIOS[0]
    r = process_raw(as_printed(s), "sha-a", s["file"], fresh_store(), today=TODAY, policy=Policy(approval_limit="1000"))
    assert r.decision.outcome == "REVIEW" and rule(r, "approval_limit").outcome == "fail"


def test_locked_controls_ignore_policy():
    """No policy setting can let a bank-account change through."""
    s = next(x for x in SCENARIOS if x["file"].startswith("07"))
    lenient = Policy(unknown_vendor_action="review", near_duplicate_action="review", stale_invoice_action="note",
                     amount_tolerance="5", po_overrun_pct="10")
    r = process_raw(as_printed(s), "sha-l", s["file"], fresh_store(), today=TODAY, policy=lenient)
    assert r.decision.outcome == "REVIEW" and rule(r, "bank_details").action == "review"


@pytest.mark.parametrize("printed,clean", [
    ("IN GST 9925USA29095OS3", "9925USA29095OS3"), ("VAT No: DE318877465", "DE318877465"),
    ("DE318877465", "DE318877465"), ("Tax ID: 84-2231907", "84-2231907"), ("EIN 12-3456789", "12-3456789"),
    ("985-73-8194", "985-73-8194"),
])
def test_tax_id_label_stripped(printed, clean):
    from intel_doc.transform import clean_tax_id
    assert clean_tax_id(printed) == clean
