"""Local OCR for scanned invoices (free, CPU-only, no API).

RapidOCR returns word boxes. We rebuild the page as text rows - boxes whose
vertical centres line up become one row, joined left to right with ' | ' - so
the LLM sees table structure instead of a word soup.
"""
from __future__ import annotations

import statistics
from functools import lru_cache


@lru_cache(maxsize=1)
def _engine():
    from rapidocr_onnxruntime import RapidOCR  # imported lazily: model load takes a few seconds
    return RapidOCR()


def ocr_png(png: bytes) -> tuple[str, float]:
    """Returns (layout text, mean confidence 0..1)."""
    result, _ = _engine()(png)
    if not result:
        return "", 0.0
    boxes = []
    for pts, text, conf in result:
        ys = [p[1] for p in pts]
        boxes.append({"x": min(p[0] for p in pts), "y": (min(ys) + max(ys)) / 2, "h": max(ys) - min(ys),
                      "text": text, "conf": float(conf)})
    tol = statistics.median(b["h"] for b in boxes) * 0.5
    rows: list[list[dict]] = []
    for b in sorted(boxes, key=lambda b: b["y"]):
        if rows and abs(rows[-1][0]["y"] - b["y"]) <= tol:
            rows[-1].append(b)
        else:
            rows.append([b])
    text = "\n".join(" | ".join(b["text"] for b in sorted(r, key=lambda b: b["x"])) for r in rows)
    return text, statistics.mean(b["conf"] for b in boxes)
