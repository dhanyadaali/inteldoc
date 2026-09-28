"""Create the schema and load master data (vendors + purchase orders).

Vendors come from two places:
  * the Hugging Face dataset's ground truth (real-looking sellers with tax id + IBAN)
  * the scenario vendors used by scripts/generate_invoices.py

    python scripts/seed.py            # create tables + master data
    python scripts/seed.py --reset    # also wipe processed invoices / run history
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from generate_invoices import BRIGHTLINE, NORTHWIND, PURCHASE_ORDERS  # noqa: E402
from intel_doc.store import PgStore  # noqa: E402
from intel_doc.transform import iban_valid, norm_id  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete all processed invoices and runs")
    args = ap.parse_args()

    store = PgStore()
    store.init_schema()
    if args.reset:
        store.reset_invoices()
        print("Cleared invoices and run history")

    ids = {}
    for v in (NORTHWIND, BRIGHTLINE):
        ids[v["tax_id"]] = store.upsert_vendor(v["name"], v["tax_id"], v["iban"], v["currency"])
    for po in PURCHASE_ORDERS:
        store.upsert_po(po["po_number"], ids[po["vendor_tax_id"]], po["currency"], po["amount"])

    gt_file = ROOT / "data" / "dataset" / "ground_truth.json"
    n, bad = 0, []
    if gt_file.exists():
        for name, gt in json.loads(gt_file.read_text()).items():
            h = gt.get("header", {})
            if h.get("seller_tax_id"):
                # Some dataset labels have corrupted IBANs (fail mod-97 or wrong length).
                # Never put an unverified bank account into master data.
                iban = norm_id(h.get("iban"))
                if iban and not iban_valid(iban):
                    bad.append(name)
                    iban = None
                store.upsert_vendor(h["seller"], h["seller_tax_id"], iban, "USD")
                n += 1
    else:
        print("(no dataset yet - run scripts/fetch_dataset.py to add dataset vendors)")
    print(f"Seeded {2 + n} vendors, {len(PURCHASE_ORDERS)} purchase order(s)")
    if bad:
        print(f"  {len(bad)} dataset label(s) had an invalid IBAN - stored without bank details: {', '.join(bad)}")


if __name__ == "__main__":
    main()
