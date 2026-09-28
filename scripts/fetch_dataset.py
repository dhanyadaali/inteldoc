"""Download real invoice images + ground truth from Hugging Face.

Dataset: katanaml-org/invoices-donut-data-v1 (MIT, 501 invoices, labelled).
We use the 26-invoice test split: small, and enough to measure extraction accuracy.

    python scripts/fetch_dataset.py [--limit 26]   -> data/dataset/*.png + ground_truth.json
"""
from __future__ import annotations

import argparse
import io
import json
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

REPO = "katanaml-org/invoices-donut-data-v1"
API = f"https://huggingface.co/api/datasets/{REPO}/tree/main/data"
OUT = Path(__file__).resolve().parent.parent / "data" / "dataset"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test")
    ap.add_argument("--limit", type=int, default=26)
    args = ap.parse_args()

    files = json.load(urllib.request.urlopen(API))
    name = next(f["path"] for f in files if f["path"].split("/")[-1].startswith(args.split + "-"))
    url = f"https://huggingface.co/datasets/{REPO}/resolve/main/{name}"
    print(f"Downloading {url}")
    table = pq.read_table(io.BytesIO(urllib.request.urlopen(url).read()))

    OUT.mkdir(parents=True, exist_ok=True)
    truth = {}
    for i, row in enumerate(table.to_pylist()[: args.limit]):
        fname = f"hf_{args.split}_{i:03d}.png"
        (OUT / fname).write_bytes(row["image"]["bytes"])
        truth[fname] = json.loads(row["ground_truth"])["gt_parse"]
    (OUT / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    print(f"Wrote {len(truth)} invoices to {OUT}")


if __name__ == "__main__":
    main()
