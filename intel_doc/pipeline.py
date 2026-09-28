"""The ETL pipeline: Extract -> Transform -> Validate -> Decide -> Load.

Every step is timed and recorded, so the result carries a full trace of what
happened between "invoice in" and "decision out".
"""
from __future__ import annotations

import json
import time
from typing import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .decide import decide
from .extract import Extractor, load_document
from .models import Decision, Finding, Invoice, RawExtraction
from .policy import Policy
from .store import Store
from .transform import transform
from .validate import run_all


@dataclass
class Result:
    source_file: str
    invoice_id: int | None
    decision: Decision
    invoice: Invoice
    findings: list[Finding]
    steps: list[dict] = field(default_factory=list)
    run_id: int | None = None


class Trace:
    """Records each step; `on_event` (if given) receives start/end events live - the UI streams these."""

    def __init__(self, on_event: Callable[[dict], None] | None = None):
        self.steps: list[dict] = []
        self.on_event = on_event or (lambda e: None)

    def emit(self, type_: str, **data) -> None:
        self.on_event({"type": type_, **data})

    @contextmanager
    def step(self, stage: str, name: str):
        detail: dict = {}
        t0 = time.perf_counter()
        self.emit("step_start", stage=stage, name=name)
        try:
            yield detail
            status = "ok"
        except Exception as e:
            detail["error"] = f"{type(e).__name__}: {e}"
            status = "error"
            raise
        finally:
            step = {"stage": stage, "name": name, "status": status, "detail": detail,
                    "duration_ms": int((time.perf_counter() - t0) * 1000)}
            self.steps.append(step)
            self.emit("step_end", **json.loads(json.dumps(step, default=str)))


def process_file(path: str | Path, store: Store, extractor: Extractor, today: date | None = None,
                 trace: Trace | None = None, display_name: str | None = None) -> Result:
    """Run the full pipeline. If any stage crashes, the failed run is still recorded, then re-raised."""
    trace = trace or Trace()
    name = display_name or Path(path).name
    try:
        return _process_file(path, name, store, extractor, today, trace)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        run_id = store.save_failed(source_file=name, steps=trace.steps, error=err, model=getattr(extractor, "model", None),
                          document_path=str(path))
        trace.emit("failed", error=err, run_id=run_id)
        raise


def _process_file(path, name, store, extractor, today, trace) -> Result:
    with trace.step("extract", "load_document") as d:
        doc = load_document(path)
        d.update(file=name, sha256=doc.sha256[:12], pages=len(doc.images_b64),
                 text_layer=bool(doc.text))
    if doc.needs_ocr:
        with trace.step("extract", "ocr") as d:
            doc.run_ocr()
            d.update(engine="RapidOCR (local)", chars=len(doc.ocr_text), min_confidence=doc.ocr_confidence,
                     preview=doc.ocr_text[:400])
    with trace.step("extract", "llm_extraction") as d:
        raw, meta = extractor.extract(doc)
        d.update(meta)
        d["unclear_fields"] = raw.unclear_fields
    trace.emit("extracted", raw=raw.model_dump())
    return process_raw(raw, doc.sha256, name, store, today=today, trace=trace, model=extractor.model,
                       document_path=str(path))


def process_raw(raw: RawExtraction, sha256: str, source_file: str, store: Store, *, today: date | None = None,
                trace: Trace | None = None, model: str | None = None, document_path: str | None = None,
                policy: Policy | None = None) -> Result:
    """Everything after extraction. Tests and the evaluator enter here directly."""
    trace = trace or Trace()
    if policy is None:
        policy, version = store.get_policy()
    else:
        version = None
    with trace.step("transform", "normalise") as d:
        inv, notes = transform(raw, policy.date_order)
        d.update(invoice_number=inv.invoice_number, normalised=inv.invoice_number_norm,
                 currency=inv.currency, total=str(inv.total), lines=len(inv.lines), notes=notes,
                 document_type=inv.document_type)
    trace.emit("normalised", invoice=inv.model_dump(mode="json"), notes=notes)
    with trace.step("validate", "rules") as d:
        vendor, findings = run_all(inv, sha256, store, today=today, policy=policy)
        d["results"] = {f.rule: f.outcome for f in findings}
        d["policy_version"] = version
        for f in findings:
            trace.emit("finding", **f.model_dump())
    with trace.step("decide", "policy") as d:
        decision = decide(findings)
        d.update(outcome=decision.outcome, summary=decision.summary)
    arithmetic = next(f for f in findings if f.rule == "arithmetic")
    mode = arithmetic.evidence.get("mode")
    with trace.step("load", "persist") as d:
        inv_id, run_id = store.save(source_file=source_file, sha256=sha256, inv=inv, vendor=vendor,
                            tax_inclusive=None if mode is None else mode == "tax_inclusive",
                            decision=decision, findings=findings, steps=trace.steps,
                            raw_extraction=raw.model_dump(), model=model, document_path=document_path,
                            policy=policy, policy_version=version)
        d.update(invoice_id=inv_id, run_id=run_id)
    trace.emit("done", invoice_id=inv_id, run_id=run_id, **decision.model_dump())
    return Result(source_file, inv_id, decision, inv, findings, trace.steps, run_id)
