"""Web app: upload -> live run view (SSE) -> dashboard.

    uvicorn web.server:app --reload

One background worker processes jobs strictly in order. That matters: duplicate
and PO checks depend on what was processed before, so two invoices must never
race each other.
"""
from __future__ import annotations

import json
import queue
from functools import lru_cache
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from decimal import Decimal

from pydantic import BaseModel, ValidationError

import pymupdf

from intel_doc import config
from intel_doc.extract import Extractor
from intel_doc.pipeline import Trace, process_file
from intel_doc.policy import LOCKED_CONTROLS, Policy
from intel_doc.store import PgStore
from intel_doc.transform import iban_valid, norm_id

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UPLOADS = DATA / "uploads"
SAMPLE_DIRS = {"scenarios": DATA / "generated", "dataset": DATA / "dataset"}
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 10 * 1024 * 1024

app = FastAPI(title="IntelDoc")

# Optional protection for a public link (off when DEMO_PASSWORD is unset, so local use is unchanged):
#   DEMO_PASSWORD                    owner - any username, full access
#   GUEST_USERNAME + GUEST_PASSWORD  e.g. for interviewers - everything except wiping history
_ENV = __import__("os").environ
_DEMO_PASSWORD = _ENV.get("DEMO_PASSWORD", "")
_GUEST_USER, _GUEST_PASSWORD = _ENV.get("GUEST_USERNAME", ""), _ENV.get("GUEST_PASSWORD", "")
OWNER_ONLY = {("POST", "/api/reset")}


def _role(auth_header: str) -> str | None:
    """'owner', 'guest', or None for wrong/missing credentials."""
    import base64
    import secrets
    if not auth_header.startswith("Basic "):
        return None
    try:
        user, _, password = base64.b64decode(auth_header[6:]).decode().partition(":")
    except Exception:
        return None
    if _GUEST_USER and _GUEST_PASSWORD and secrets.compare_digest(user, _GUEST_USER) \
            and secrets.compare_digest(password, _GUEST_PASSWORD):
        return "guest"
    if secrets.compare_digest(password, _DEMO_PASSWORD):
        return "owner"
    return None


@app.middleware("http")
async def demo_password(request, call_next):
    # /api/config stays open: hosting health checks call it without a password (it only shows the model name)
    if _DEMO_PASSWORD and request.url.path != "/api/config":
        role = _role(request.headers.get("authorization", ""))
        if role is None:
            return Response("Password required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="IntelDoc demo"'})
        if role == "guest" and (request.method, request.url.path) in OWNER_ONLY:
            return Response('{"detail":"Only the owner can reset history"}', status_code=403,
                            media_type="application/json")
        request.state.role = role
    return await call_next(request)


@app.get("/api/me")
def me(request: Request):
    return {"role": getattr(request.state, "role", "owner")}


# ------------------------------------------------------------------ jobs
@dataclass
class Job:
    id: str
    name: str
    path: Path
    rerun_of: int | None = None
    events: list[dict] = field(default_factory=list)
    done: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)

    def push(self, event: dict) -> None:
        event["ts"] = time.time()  # lets the UI replay a finished run with true timings
        with self.cond:
            self.events.append(event)
            if event["type"] in ("done", "failed"):
                self.done = True
            self.cond.notify_all()


JOBS: dict[str, Job] = {}
QUEUE: "queue.Queue[Job]" = queue.Queue()
_reader_lock = threading.Lock()
_reader: PgStore | None = None


def reader() -> PgStore:
    """Shared connection for read endpoints (the worker has its own)."""
    global _reader
    if _reader is None:
        _reader = PgStore()
        _reader.init_schema()
    _reader.ensure_alive()
    return _reader


def worker() -> None:
    store = PgStore()
    extractor: Extractor | None = None
    while True:
        job = QUEUE.get()
        job.push({"type": "started", "name": job.name})
        trace = Trace(on_event=job.push)
        try:
            store.ensure_alive()
            extractor = extractor or Extractor()
            result = process_file(job.path, store, extractor, trace=trace, display_name=job.name)
            if job.rerun_of:
                store.finish_rerun(job.rerun_of, result.run_id)
        except Exception as e:  # process_file already recorded + emitted the failure
            if job.rerun_of:
                store.finish_rerun(job.rerun_of, None)
            if not job.done:
                err = f"{type(e).__name__}: {e}"
                run_id = store.save_failed(source_file=job.name, steps=trace.steps, error=err,
                                           model=config.OPENAI_MODEL, document_path=str(job.path))
                job.push({"type": "failed", "error": err, "run_id": run_id})


threading.Thread(target=worker, daemon=True).start()


def _warm_ocr() -> None:
    """Load the OCR model at startup so the first scan in a demo isn't slowed by it."""
    from intel_doc.ocr import _engine
    _engine()


# Pre-loading OCR costs ~300 MB RAM; small free hosts (Render: 512 MB) set OCR_WARMUP=false so the
# engine only loads when a scan actually arrives.
if __import__("os").getenv("OCR_WARMUP", "true").lower() == "true":
    threading.Thread(target=_warm_ocr, daemon=True).start()


def enqueue(name: str, path: Path, rerun_of: int | None = None) -> Job:
    job = Job(uuid.uuid4().hex[:10], name, path, rerun_of)
    JOBS[job.id] = job
    job.push({"type": "queued", "name": name, "position": QUEUE.qsize() + 1})
    QUEUE.put(job)
    return job


# ------------------------------------------------------------------ run endpoints
@app.post("/api/runs/upload")
async def upload(files: list[UploadFile] = File(...)):
    UPLOADS.mkdir(parents=True, exist_ok=True)
    jobs = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED:
            raise HTTPException(400, f"{f.filename}: only PDF or image files are accepted")
        data = await f.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(400, f"{f.filename}: larger than 10 MB")
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(f.filename).name)
        path = UPLOADS / f"{uuid.uuid4().hex[:8]}_{safe}"
        path.write_bytes(data)
        jobs.append(enqueue(Path(f.filename).name, path))
    return [{"job_id": j.id, "name": j.name} for j in jobs]


class SampleRequest(BaseModel):
    samples: list[str]  # "scenarios/01_happy_path.pdf"


@app.post("/api/runs/samples")
def run_samples(req: SampleRequest):
    jobs = []
    for s in req.samples:
        path = _sample_path(s)
        jobs.append(enqueue(path.name, path))
    return [{"job_id": j.id, "name": j.name} for j in jobs]


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")

    def stream():
        sent = 0
        while True:
            with job.cond:
                while sent >= len(job.events) and not job.done:
                    if not job.cond.wait(timeout=15):
                        break
                new, finished = job.events[sent:], job.done
            if not new and not finished:
                yield ": keep-alive\n\n"
                continue
            for e in new:
                yield f"data: {json.dumps(e, default=str)}\n\n"
            sent += len(new)
            if finished and sent >= len(job.events):
                return

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ dashboard endpoints
@app.get("/api/runs")
def runs(limit: int = 200):
    with _reader_lock:
        return reader().list_runs(limit)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    with _reader_lock:
        d = reader().run_detail(run_id)
    if not d:
        raise HTTPException(404, "run not found")
    return d


@app.get("/api/runs/{run_id}/document")
def run_document(run_id: int):
    with _reader_lock:
        d = reader().run_detail(run_id)
    p = Path(d["run"]["document_path"]) if d and d["run"]["document_path"] else None
    if not p or not p.exists() or DATA.resolve() not in p.resolve().parents:
        raise HTTPException(404, "document not available")
    return FileResponse(p)


@app.get("/api/runs/{run_id}/preview.png")
def run_preview(run_id: int):
    with _reader_lock:
        d = reader().run_detail(run_id)
    p = Path(d["run"]["document_path"]) if d and d["run"]["document_path"] else None
    if not p or not p.exists() or DATA.resolve() not in p.resolve().parents:
        raise HTTPException(404, "document not available")
    return _preview(p)


@app.get("/api/jobs/{job_id}/preview.png")
def job_preview(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.path.exists():
        raise HTTPException(404, "unknown job")
    return _preview(job.path)


def _preview(path: Path) -> Response:
    """First page as PNG - identical rendering in every browser, no PDF plug-in needed."""
    return Response(_render_png(str(path), path.stat().st_mtime), media_type="image/png",
                    headers={"Cache-Control": "max-age=3600"})


@lru_cache(maxsize=64)
def _render_png(path: str, _mtime: float) -> bytes:
    if path.lower().endswith(".pdf"):
        return pymupdf.open(path)[0].get_pixmap(dpi=110).tobytes("png")
    pix = pymupdf.Pixmap(path)
    if pix.alpha or pix.n > 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    return pix.tobytes("png")


@app.get("/api/stats")
def stats():
    with _reader_lock:
        return reader().stats()


@app.get("/api/master")
def master():
    with _reader_lock:
        return reader().master_data()


class Resolution(BaseModel):
    resolution: str  # APPROVE | REJECT
    note: str = ""


@app.post("/api/invoices/{invoice_id}/resolve")
def resolve(invoice_id: int, body: Resolution):
    if body.resolution not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "resolution must be APPROVE or REJECT")
    with _reader_lock:
        if not reader().resolve(invoice_id, body.resolution, body.note.strip()):
            raise HTTPException(409, "only invoices awaiting review can be resolved")
    return {"ok": True}


@app.post("/api/runs/{run_id}/rerun")
def rerun(run_id: int):
    """Process the same document again (e.g. after onboarding the vendor or changing policy).
    The earlier result is superseded, so the document doesn't count as its own duplicate."""
    with _reader_lock:
        r = reader().begin_rerun(run_id)
    if not r or not r["document_path"] or not Path(r["document_path"]).exists():
        raise HTTPException(404, "run or its document not available")
    job = enqueue(r["source_file"], Path(r["document_path"]), rerun_of=run_id)
    return {"job_id": job.id, "name": job.name}


# ------------------------------------------------------------------ customisation
@app.get("/api/policy")
def get_policy():
    with _reader_lock:
        policy, version = reader().get_policy()
        history = reader().policy_history()
    return {"settings": json.loads(policy.model_dump_json()), "version": version,
            "defaults": json.loads(Policy().model_dump_json()),
            "schema": Policy.model_json_schema()["properties"], "locked": LOCKED_CONTROLS, "history": history}


class PolicyUpdate(BaseModel):
    settings: dict
    note: str = ""


@app.put("/api/policy")
def put_policy(body: PolicyUpdate):
    try:
        policy = Policy.model_validate(body.settings)  # bounds + allowed choices enforced here
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    with _reader_lock:
        version = reader().save_policy(policy, body.note.strip())
    return {"version": version}


class NewVendor(BaseModel):
    name: str
    tax_id: str
    iban: str = ""
    currency: str = "USD"


@app.post("/api/vendors")
def create_vendor(v: NewVendor):
    name, tax_id, currency = v.name.strip(), v.tax_id.strip(), v.currency.strip().upper()
    iban = norm_id(v.iban) or None
    if not name or not tax_id:
        raise HTTPException(400, "name and tax id are required")
    if iban and not iban_valid(iban):
        raise HTTPException(400, "IBAN fails its checksum - check it with the vendor before saving")
    if len(currency) != 3:
        raise HTTPException(400, "currency must be a 3-letter code")
    with _reader_lock:
        try:
            return {"id": reader().create_vendor(name, tax_id, iban, currency)}
        except ValueError as e:
            raise HTTPException(409, str(e))


class NewPO(BaseModel):
    po_number: str
    vendor_id: int
    amount: Decimal


@app.post("/api/pos")
def create_po(po: NewPO):
    number = po.po_number.strip().upper()
    if not number or po.amount <= 0:
        raise HTTPException(400, "PO number and a positive amount are required")
    with _reader_lock:
        try:
            reader().create_po(number, po.vendor_id, po.amount)
        except ValueError as e:
            raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/reset")
def reset():
    """Demo helper: forget processed invoices (master data stays)."""
    with _reader_lock:
        reader().reset_invoices()
    return {"ok": True}


# ------------------------------------------------------------------ samples
def _sample_path(s: str) -> Path:
    kind, _, name = s.partition("/")
    base = SAMPLE_DIRS.get(kind)
    path = (base / name) if base else None
    if not path or not path.is_file() or path.resolve().parent != base.resolve():
        raise HTTPException(404, f"unknown sample {s}")
    return path


@app.get("/api/samples")
def samples():
    out = []
    exp_file = SAMPLE_DIRS["scenarios"] / "expected.json"
    expected = json.loads(exp_file.read_text()) if exp_file.exists() else {}
    for name, e in expected.items():
        out.append({"id": f"scenarios/{name}", "kind": "scenarios", "name": name, **e})
    gt_file = SAMPLE_DIRS["dataset"] / "ground_truth.json"
    if gt_file.exists():
        for name, gt in json.loads(gt_file.read_text()).items():
            h = gt.get("header", {})
            out.append({"id": f"dataset/{name}", "kind": "dataset", "name": name,
                        "why": f"Hugging Face scan - invoice {h.get('invoice_no')} from {h.get('seller', '')[:40]}"})
    return out


@app.get("/api/samples/{kind}/{name}")
def sample_file(kind: str, name: str):
    return FileResponse(_sample_path(f"{kind}/{name}"))


@app.get("/api/config")
def cfg():
    return {"model": config.OPENAI_MODEL, "endpoint": config.OPENAI_BASE_URL, "vision": config.LLM_VISION,
            "has_key": bool(config.OPENAI_API_KEY)}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
