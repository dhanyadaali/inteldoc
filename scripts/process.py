"""CLI: run invoices through the pipeline and print the decision + trace.

    python scripts/process.py data/generated/01_happy_path.pdf
    python scripts/process.py data/generated/          # every file, in name order
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intel_doc.extract import Extractor  # noqa: E402
from intel_doc.pipeline import process_file  # noqa: E402
from intel_doc.store import PgStore  # noqa: E402

ICON = {"APPROVE": "[OK]", "REVIEW": "[??]", "REJECT": "[XX]"}
DOCS = {".pdf", ".png", ".jpg", ".jpeg"}


def main(args: list[str]) -> None:
    files = []
    for a in args:
        p = Path(a)
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in DOCS) if p.is_dir() else [p]
    store, extractor = PgStore(), Extractor()
    for f in files:
        r = process_file(f, store, extractor)
        print(f"\n{ICON[r.decision.outcome]} {f.name}: {r.decision.outcome} - {r.decision.summary}")
        for s in r.steps:
            print(f"     {s['stage']:<9} {s['name']:<16} {s['status']:<5} {s['duration_ms']:>6} ms")
        for reason in r.decision.reasons:
            print(f"     - {reason}")


if __name__ == "__main__":
    main(sys.argv[1:] or ["data/generated"])
