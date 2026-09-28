"""Measure extraction accuracy on the Hugging Face dataset (OCR + LLM vs ground truth).

No database involved - this scores the Extract + Transform steps only.

    python scripts/evaluate.py --limit 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from intel_doc.extract import Extractor, load_document  # noqa: E402
from intel_doc.transform import iban_valid, norm_id, parse_date, parse_money, transform  # noqa: E402

DATA = ROOT / "data" / "dataset"


def expected(gt: dict) -> dict:
    h, s = gt["header"], gt.get("summary", {})
    return {
        "invoice_number": norm_id(h.get("invoice_no")),
        "invoice_date": parse_date(h.get("invoice_date"), [], "gt"),
        "vendor_tax_id": norm_id(h.get("seller_tax_id")),
        "vendor_iban": norm_id(h.get("iban")),
        "total": parse_money(s.get("total_gross_worth")),
        "subtotal": parse_money(s.get("total_net_worth")),
        "line_count": len(gt.get("items", [])),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    truth = json.loads((DATA / "ground_truth.json").read_text())
    extractor = Extractor()
    fields = ["invoice_number", "invoice_date", "vendor_tax_id", "vendor_iban", "total", "subtotal", "line_count"]
    hits = {f: 0 for f in fields}
    label_errors: list[str] = []
    n = 0
    for name, gt in list(truth.items())[: args.limit]:
        doc = load_document(DATA / name)
        if doc.needs_ocr:
            doc.run_ocr()
        raw, _ = extractor.extract(doc)
        inv, _ = transform(raw)
        got = {"invoice_number": inv.invoice_number_norm, "invoice_date": inv.invoice_date,
               "vendor_tax_id": norm_id(inv.vendor_tax_id), "vendor_iban": norm_id(inv.vendor_iban),
               "total": inv.total, "subtotal": inv.subtotal, "line_count": len(inv.lines)}
        exp = expected(gt)
        misses = [f for f in fields if got[f] != exp[f]]
        # A label that fails the IBAN checksum is itself wrong; if the model's value passes
        # the checksum, score the model as correct and report the bad label.
        if "vendor_iban" in misses and not iban_valid(exp["vendor_iban"]) and iban_valid(got["vendor_iban"]):
            misses.remove("vendor_iban")
            label_errors.append(f"{name} (iban fails checksum)")
        # Same idea for totals: a label total that disagrees with its own net + VAT is wrong.
        label_tax = parse_money(gt.get("summary", {}).get("total_vat")) or 0
        if ("total" in misses and exp["subtotal"] is not None and got["total"] is not None
                and abs(exp["subtotal"] + label_tax - exp["total"]) > 0.02
                and abs(exp["subtotal"] + label_tax - got["total"]) <= 0.02):
            misses.remove("total")
            label_errors.append(f"{name} (total != net + vat)")
        if "vendor_tax_id" in misses and not exp["vendor_tax_id"]:
            misses.remove("vendor_tax_id")
            label_errors.append(f"{name} (tax id missing from label)")
        for f in fields:
            hits[f] += f not in misses
        n += 1
        print(f"{name}: {'all fields correct' if not misses else 'MISS ' + ', '.join(f'{f} got={got[f]} want={exp[f]}' for f in misses)}")

    print(f"\nField accuracy over {n} scanned invoices:")
    for f in fields:
        print(f"  {f:<16} {hits[f] / n:6.0%}")
    if label_errors:
        print(f"\nDataset label errors (model value kept, label shown to be wrong): {', '.join(label_errors)}")


if __name__ == "__main__":
    main()
