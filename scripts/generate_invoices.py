"""Invoice generator: renders the happy-path + edge-case scenario invoices as PDFs.

Files are numbered because ORDER MATTERS - several edge cases only exist
because of what was processed before them (duplicates, PO consumption).

    python scripts/generate_invoices.py            -> data/generated/*.pdf + expected.json
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pymupdf as fitz

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "generated"

# Master data these scenarios are written against (seeded by scripts/seed.py).
NORTHWIND = dict(name="Northwind Office Supplies Inc.", tax_id="84-2231907",
                 iban="GB29NWBK60161331926819", currency="USD",
                 address="1200 Harbor Blvd, Oakland, CA 94607")
BRIGHTLINE = dict(name="Brightline Consulting GmbH", tax_id="DE318877465",
                  iban="DE89370400440532013000", currency="EUR",
                  address="Friedrichstrasse 68, 10117 Berlin")
FRAUD_IBAN = "GB33BUKB20201555555555"
PURCHASE_ORDERS = [dict(po_number="PO-7781", vendor_tax_id=NORTHWIND["tax_id"], currency="USD", amount="10000.00")]

CLIENT = "Acme Manufacturing Ltd.\n45 Industrial Way, Austin, TX 78701"


def money(x: Decimal, cur: str) -> str:
    if cur == "EUR":  # European formatting on purpose: 1.234,56 EUR
        s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{s} EUR"
    return f"${x:,.2f}"


def scenario(file, expected, why, vendor, number, date, lines, tax_rate, *, due=None, po=None, iban=None,
             inclusive=False, total_override=None, note=None, title="INVOICE", date_label="Invoice date"):
    lines = [(d, Decimal(q), Decimal(p)) for d, q, p in lines]
    line_amounts = [(q * p).quantize(Decimal("0.01")) for _, q, p in lines]
    rate = Decimal(tax_rate)
    if inclusive:
        total = sum(line_amounts)
        subtotal = (total / (1 + rate)).quantize(Decimal("0.01"))
        tax = total - subtotal
    else:
        subtotal = sum(line_amounts)
        tax = (subtotal * rate).quantize(Decimal("0.01"))
        total = subtotal + tax
    if total_override is not None:
        total = Decimal(total_override)
    return dict(file=file, expected=expected, why=why, vendor=vendor, number=number, date=date, due=due, po=po,
                iban=iban or vendor["iban"], lines=lines, line_amounts=line_amounts, subtotal=subtotal, tax=tax,
                total=total, rate=rate, inclusive=inclusive, note=note, title=title, date_label=date_label)


SCENARIOS = [
    scenario("01_happy_path.pdf", "APPROVE", "Clean invoice from a known vendor; everything reconciles.",
             NORTHWIND, "INV-2026-0101", "09/02/2026",
             [("A4 copy paper, 80gsm (box of 5 reams)", "20", "42.50"), ("Toner cartridge HP 26X", "6", "129.00")],
             "0.0825", due="10/02/2026"),

    # --- Edge case: cumulative PO overrun (3 partial deliveries against one PO)
    scenario("02_po_partial_1.pdf", "APPROVE", "First partial delivery against PO-7781 (4,000 of 10,000).",
             NORTHWIND, "INV-2026-0102", "09/05/2026", [("Ergonomic office chair", "16", "250.00")], "0",
             po="PO-7781", due="10/05/2026"),
    scenario("03_po_partial_2.pdf", "APPROVE", "Second partial delivery (8,500 of 10,000 used).",
             NORTHWIND, "INV-2026-0103", "09/12/2026", [("Standing desk, 140cm", "10", "450.00")], "0",
             po="PO-7781", due="10/12/2026"),
    scenario("04_po_partial_3_overrun.pdf", "REVIEW",
             "Third partial delivery. Fine on its own (2,400 < 10,000) but takes the PO to 10,900 - "
             "only visible by looking at what was already billed.",
             NORTHWIND, "INV-2026-0104", "09/19/2026", [("Monitor arm, dual", "12", "200.00")], "0",
             po="PO-7781", due="10/19/2026"),

    # --- Edge case: disguised duplicates
    scenario("05_duplicate_reformatted.pdf", "REJECT",
             "Same invoice as 01 re-sent with the number written differently ('INV 2026/0101'). "
             "A string-equality check would miss it.",
             NORTHWIND, "INV 2026/0101", "Sep 2, 2026",
             [("A4 copy paper, 80gsm (box of 5 reams)", "20", "42.50"), ("Toner cartridge HP 26X", "6", "129.00")],
             "0.0825", due="Oct 2, 2026", title="TAX INVOICE", date_label="Date"),
    scenario("06_resubmitted_new_number.pdf", "REVIEW",
             "Same goods and same amount as 01, new invoice number, dated 5 days later. Could be a "
             "legitimate repeat order or a double-bill - a human should decide.",
             NORTHWIND, "INV-2026-0111", "09/07/2026",
             [("A4 copy paper, 80gsm (box of 5 reams)", "20", "42.50"), ("Toner cartridge HP 26X", "6", "129.00")],
             "0.0825", due="10/07/2026"),

    # --- Edge case: bank detail swap (business email compromise)
    scenario("07_bank_details_changed.pdf", "REVIEW",
             "Perfect invoice from a known vendor, but the IBAN is not the one on file and there is a "
             "'new bank details' note. Classic payment-fraud pattern.",
             NORTHWIND, "INV-2026-0107", "09/20/2026", [("Whiteboard markers (pack of 12)", "30", "14.99")],
             "0.0825", iban=FRAUD_IBAN, due="10/20/2026",
             note="IMPORTANT: Please note our NEW bank details below. Kindly update your records."),

    # --- Edge case: tax-inclusive pricing vs. a real arithmetic error
    scenario("08_vat_inclusive.pdf", "APPROVE",
             "German vendor quoting gross prices (VAT included). Lines sum to the TOTAL, not the net - "
             "a naive lines==subtotal check would wrongly flag it.",
             BRIGHTLINE, "BL-2026-0412", "15.09.2026",
             [("Process audit workshop (day rate)", "2", "850.00"), ("Travel expenses, flat", "1", "300.00")],
             "0.19", inclusive=True, due="15.10.2026",
             note="All prices include 19% VAT (Preise inkl. MwSt.)."),
    scenario("09_math_error.pdf", "REVIEW",
             "Same vendor, net pricing, but the total is overstated by 100.00. Must still be caught - "
             "the tax-inclusive fallback must not make the check lenient.",
             BRIGHTLINE, "BL-2026-0419", "22.09.2026",
             [("Process audit workshop (day rate)", "1", "850.00"), ("Report write-up", "1", "650.00")],
             "0.19", total_override="1885.00", due="22.10.2026"),
]


def render(s: dict, path: Path) -> None:
    v, cur = s["vendor"], s["vendor"]["currency"]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    y = 60

    def text(x, y, t, size=10, bold=False, color=(0, 0, 0)):
        page.insert_text((x, y), t, fontsize=size, fontname="helv" if not bold else "hebo", color=color)

    text(50, y, v["name"], 16, True)
    text(50, y + 18, v["address"])
    text(50, y + 32, f"Tax ID: {v['tax_id']}")
    text(420, y, s["title"], 18, True, (0.15, 0.3, 0.6))
    y += 70
    text(50, y, "Bill to:", bold=True)
    for i, ln in enumerate(CLIENT.split("\n")):
        text(50, y + 14 * (i + 1), ln)
    meta = [("Invoice no.", s["number"]), (s["date_label"], s["date"])]
    if s["due"]:
        meta.append(("Due date", s["due"]))
    if s["po"]:
        meta.append(("PO number", s["po"]))
    for i, (k, val) in enumerate(meta):
        text(350, y + 14 * i, f"{k}:", bold=True)
        text(440, y + 14 * i, val)
    y += 80

    cols = [50, 330, 390, 480]
    page.draw_rect(fitz.Rect(45, y - 12, 550, y + 4), color=None, fill=(0.9, 0.92, 0.96))
    unit_hdr = "Unit (gross)" if s["inclusive"] else "Unit price"
    for x, h in zip(cols, ["Description", "Qty", unit_hdr, "Amount"]):
        text(x, y, h, bold=True)
    y += 20
    for (desc, q, p), amt in zip(s["lines"], s["line_amounts"]):
        text(cols[0], y, desc)
        text(cols[1], y, f"{q:g}")
        text(cols[2], y, money(p, cur))
        text(cols[3], y, money(amt, cur))
        y += 16
    y += 10
    page.draw_line((330, y), (550, y))
    y += 16
    pct = f"{float(s['rate'] * 100):g}%"
    rows = [("Net amount" if s["inclusive"] else "Subtotal", s["subtotal"]),
            (f"{'incl. ' if s['inclusive'] else ''}VAT {pct}" if cur == "EUR" else f"Sales tax {pct}", s["tax"]),
            ("TOTAL DUE", s["total"])]
    for k, val in rows:
        text(330, y, k, bold=k == "TOTAL DUE")
        text(cols[3], y, money(val, cur), bold=k == "TOTAL DUE")
        y += 16

    y += 30
    if s["note"]:
        text(50, y, s["note"], 10, True, (0.7, 0.1, 0.1))
        y += 20
    text(50, y, "Payment details", bold=True)
    text(50, y + 14, f"IBAN: {' '.join(s['iban'][i:i + 4] for i in range(0, len(s['iban']), 4))}")
    text(50, y + 28, "Please quote the invoice number with your payment.")
    doc.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    expected = {}
    for s in SCENARIOS:
        render(s, OUT / s["file"])
        expected[s["file"]] = {"expected": s["expected"], "why": s["why"]}
    (OUT / "expected.json").write_text(json.dumps(expected, indent=2))
    print(f"Wrote {len(SCENARIOS)} invoices to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
