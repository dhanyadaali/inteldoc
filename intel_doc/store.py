"""Data access. PgStore is the real one; MemoryStore lets tests run without Postgres.

Rejected invoices are ignored when looking at history: a rejected invoice was
never going to be paid, so it neither blocks a corrected resubmission nor
consumes PO budget. Invoices awaiting REVIEW *do* count - they may still be paid. Once a human
resolves a REVIEW, the resolution is what counts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import config
from .models import Decision, Finding, Invoice
from .transform import norm_id


@dataclass
class Vendor:
    id: int
    name: str
    tax_id: str | None
    iban: str | None
    currency: str


@dataclass
class PurchaseOrder:
    po_number: str
    vendor_id: int
    currency: str
    amount: Decimal


class Store(Protocol):
    def vendor_by_tax_id(self, tax_id: str) -> Vendor | None: ...
    def all_vendors(self) -> list[Vendor]: ...
    def po(self, po_number: str) -> PurchaseOrder | None: ...
    def po_billed(self, po_number: str) -> Decimal: ...
    def invoice_by_sha(self, sha: str) -> dict | None: ...
    def invoice_by_number(self, vendor_id: int, number_norm: str) -> dict | None: ...
    def get_policy(self) -> tuple: ...
    def similar_invoices(self, vendor_id: int, total: Decimal, around: date, window_days: int) -> list[dict]: ...
    def save(self, *, source_file: str, sha256: str, inv: Invoice, vendor: Vendor | None, tax_inclusive: bool | None,
             decision: Decision, findings: list[Finding], steps: list[dict], raw_extraction: dict,
             model: str | None, document_path: str | None = None, policy=None,
             policy_version: int | None = None) -> tuple[int, int]: ...
    def vendor_invoice_count(self, vendor_id: int) -> int: ...
    def open_pos(self, vendor_id: int) -> list[dict]: ...
    def save_failed(self, *, source_file: str, steps: list[dict], error: str, model: str | None,
                    document_path: str | None = None) -> int: ...


# ------------------------------------------------------------------ Postgres
class PgStore:
    def __init__(self, url: str | None = None):
        self.url = url or config.DATABASE_URL
        self.conn = psycopg.connect(self.url, row_factory=dict_row, autocommit=True, prepare_threshold=None)

    def ensure_alive(self) -> None:
        """Serverless Postgres (Neon) drops idle connections; reconnect if needed."""
        try:
            self.conn.execute("SELECT 1")
        except psycopg.Error:
            self.conn.close()
            self.conn = psycopg.connect(self.url, row_factory=dict_row, autocommit=True, prepare_threshold=None)

    def init_schema(self) -> None:
        sql = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()
        self.conn.execute(sql)

    def _one(self, sql, *args):
        return self.conn.execute(sql, args).fetchone()

    def vendor_by_tax_id(self, tax_id):
        r = self._one("SELECT id,name,tax_id,iban,currency FROM vendors "
                      "WHERE upper(regexp_replace(tax_id,'[^A-Za-z0-9]','','g'))=%s", norm_id(tax_id))
        return Vendor(**r) if r else None

    def all_vendors(self):
        return [Vendor(**r) for r in self.conn.execute("SELECT id,name,tax_id,iban,currency FROM vendors")]

    def po(self, po_number):
        r = self._one("SELECT po_number,vendor_id,currency,amount FROM purchase_orders WHERE po_number=%s", po_number)
        return PurchaseOrder(**r) if r else None

    def po_billed(self, po_number):
        r = self._one("SELECT COALESCE(SUM(total),0) AS s FROM invoices WHERE po_number=%s AND COALESCE(resolution,decision)<>'REJECT' AND superseded_by IS NULL",
                      po_number)
        return Decimal(r["s"])

    def invoice_by_sha(self, sha):
        return self._one("SELECT id, invoice_number, decision FROM invoices WHERE file_sha256=%s "
                         "AND superseded_by IS NULL ORDER BY id LIMIT 1", sha)

    def vendor_invoice_count(self, vendor_id):
        return self._one("SELECT count(*) AS n FROM invoices WHERE vendor_id=%s AND superseded_by IS NULL",
                         vendor_id)["n"]

    def open_pos(self, vendor_id):
        return self.conn.execute(
            """SELECT p.po_number, p.amount,
                      COALESCE((SELECT SUM(total) FROM invoices i WHERE i.po_number=p.po_number
                                AND COALESCE(i.resolution,i.decision)<>'REJECT' AND i.superseded_by IS NULL),0) AS billed
               FROM purchase_orders p WHERE p.vendor_id=%s ORDER BY p.po_number""", (vendor_id,)).fetchall()

    def invoice_by_number(self, vendor_id, number_norm):
        return self._one("""SELECT id, invoice_number, decision FROM invoices
                            WHERE vendor_id=%s AND invoice_number_norm=%s AND COALESCE(resolution,decision)<>'REJECT' AND superseded_by IS NULL
                            ORDER BY id LIMIT 1""", vendor_id, number_norm)

    def similar_invoices(self, vendor_id, total, around, window_days):
        return self.conn.execute(
            """SELECT id, invoice_number, invoice_date, total, decision FROM invoices
               WHERE vendor_id=%s AND total=%s AND COALESCE(resolution,decision)<>'REJECT' AND superseded_by IS NULL
                 AND invoice_date BETWEEN %s AND %s ORDER BY id""",
            (vendor_id, total, around - timedelta(days=window_days), around + timedelta(days=window_days)),
        ).fetchall()

    def save(self, *, source_file, sha256, inv, vendor, tax_inclusive, decision, findings, steps,
             raw_extraction, model, document_path=None, policy=None, policy_version=None):
        with self.conn.transaction():
            inv_id = self._one(
                """INSERT INTO invoices (source_file,file_sha256,vendor_id,vendor_name,invoice_number,
                       invoice_number_norm,invoice_date,due_date,currency,po_number,iban,subtotal,tax,total,
                       tax_inclusive,decision,document_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                source_file, sha256, vendor.id if vendor else None, inv.vendor_name, inv.invoice_number,
                inv.invoice_number_norm, inv.invoice_date, inv.due_date, inv.currency, inv.po_number,
                inv.vendor_iban, inv.subtotal, inv.tax, inv.total, tax_inclusive, decision.outcome,
                inv.document_type,
            )["id"]
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO invoice_lines (invoice_id,line_no,description,quantity,unit_price,amount) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [(inv_id, i, l.description, l.quantity, l.unit_price, l.amount) for i, l in enumerate(inv.lines, 1)],
                )
            run_id = self._one(
                """INSERT INTO pipeline_runs (invoice_id,source_file,document_path,model,decision,summary,
                       raw_extraction,policy_version,policy,finished_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) RETURNING id""",
                inv_id, source_file, document_path, model, decision.outcome, decision.summary, Jsonb(raw_extraction),
                policy_version, Jsonb(json.loads(policy.model_dump_json())) if policy else None,
            )["id"]
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO pipeline_steps (run_id,seq,stage,name,status,detail,duration_ms) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(run_id, i, s["stage"], s["name"], s["status"], Jsonb(json.loads(json.dumps(s["detail"], default=str))),
                      s["duration_ms"]) for i, s in enumerate(steps, 1)],
                )
                cur.executemany(
                    "INSERT INTO validation_results (run_id,rule,outcome,action,message,evidence) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [(run_id, f.rule, f.outcome, f.action, f.message, Jsonb(f.evidence)) for f in findings],
                )
        return inv_id, run_id

    def save_failed(self, *, source_file, steps, error, model, document_path=None) -> int:
        """A run that crashed (unreadable file, model outage) still shows up in history."""
        with self.conn.transaction():
            run_id = self._one(
                """INSERT INTO pipeline_runs (source_file,document_path,model,decision,summary,error,finished_at)
                   VALUES (%s,%s,%s,'FAILED',%s,%s,now()) RETURNING id""",
                source_file, document_path, model, "Processing failed - no decision made", error)["id"]
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO pipeline_steps (run_id,seq,stage,name,status,detail,duration_ms) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    [(run_id, i, s["stage"], s["name"], s["status"],
                      Jsonb(json.loads(json.dumps(s["detail"], default=str))), s["duration_ms"])
                     for i, s in enumerate(steps, 1)])
        return run_id

    # ---- used by scripts / UI
    def upsert_vendor(self, name, tax_id, iban, currency="USD") -> int:
        return self._one(
            """INSERT INTO vendors (name,tax_id,iban,currency) VALUES (%s,%s,%s,%s)
               ON CONFLICT (tax_id) DO UPDATE SET name=EXCLUDED.name, iban=EXCLUDED.iban, currency=EXCLUDED.currency
               RETURNING id""", name, tax_id, iban, currency)["id"]

    def upsert_po(self, po_number, vendor_id, currency, amount) -> None:
        self.conn.execute(
            """INSERT INTO purchase_orders (po_number,vendor_id,currency,amount) VALUES (%s,%s,%s,%s)
               ON CONFLICT (po_number) DO UPDATE SET vendor_id=EXCLUDED.vendor_id, currency=EXCLUDED.currency,
               amount=EXCLUDED.amount""", (po_number, vendor_id, currency, amount))

    def list_runs(self, limit: int = 200) -> list[dict]:
        return self.conn.execute(
            """SELECT r.id AS run_id, r.invoice_id, r.source_file, r.decision, r.summary, r.error, r.started_at,
                      i.vendor_name, i.invoice_number, i.invoice_date, i.currency, i.total, i.po_number,
                      i.resolution, COALESCE(i.resolution, r.decision) AS status,
                      r.policy_version, i.document_type,
                      (SELECT COALESCE(SUM(duration_ms),0) FROM pipeline_steps s WHERE s.run_id=r.id) AS duration_ms
               FROM pipeline_runs r LEFT JOIN invoices i ON i.id=r.invoice_id
               WHERE r.superseded_by IS NULL
               ORDER BY r.id DESC LIMIT %s""", (limit,)).fetchall()

    def stats(self) -> dict:
        by = {r["status"]: r["n"] for r in self.conn.execute(
            """SELECT COALESCE(i.resolution, r.decision) AS status, count(*) AS n
               FROM pipeline_runs r LEFT JOIN invoices i ON i.id=r.invoice_id
               WHERE r.superseded_by IS NULL GROUP BY 1""")}
        pending = self._one("SELECT count(*) AS n FROM invoices WHERE decision='REVIEW' AND resolution IS NULL "
                            "AND superseded_by IS NULL")["n"]
        rules = self.conn.execute(
            """SELECT v.rule, count(*) FILTER (WHERE v.action IN ('review','reject')) AS blocked, count(*) AS total
               FROM validation_results v JOIN pipeline_runs r ON r.id=v.run_id
               WHERE r.superseded_by IS NULL GROUP BY v.rule ORDER BY blocked DESC, v.rule""").fetchall()
        # per currency - never add USD to EUR
        money = self.conn.execute(
            """SELECT COALESCE(currency,'?') AS currency,
                      COALESCE(SUM(total) FILTER (WHERE COALESCE(resolution,decision)='APPROVE'),0) AS approved,
                      COALESCE(SUM(total) FILTER (WHERE COALESCE(resolution,decision)='REJECT'),0) AS stopped,
                      COALESCE(SUM(total) FILTER (WHERE decision='REVIEW' AND resolution IS NULL),0) AS pending
               FROM invoices WHERE superseded_by IS NULL GROUP BY 1 ORDER BY 1""").fetchall()
        avg = self._one("""SELECT COALESCE(AVG(d),0)::int AS avg_ms FROM
                           (SELECT SUM(s.duration_ms) d FROM pipeline_steps s JOIN pipeline_runs r ON r.id=s.run_id
                            WHERE r.superseded_by IS NULL GROUP BY s.run_id) x""")
        return {"by_status": by, "pending_review": pending, "total": sum(by.values()), "rules": rules,
                "amounts": money, "avg_ms": avg["avg_ms"]}

    def run_detail(self, run_id: int) -> dict | None:
        run = self._one("SELECT * FROM pipeline_runs WHERE id=%s", run_id)
        if not run:
            return None
        inv = self._one("SELECT * FROM invoices WHERE id=%s", run["invoice_id"]) if run["invoice_id"] else None
        lines = self.conn.execute("SELECT * FROM invoice_lines WHERE invoice_id=%s ORDER BY line_no",
                                  (run["invoice_id"],)).fetchall() if inv else []
        steps = self.conn.execute("SELECT * FROM pipeline_steps WHERE run_id=%s ORDER BY seq", (run_id,)).fetchall()
        findings = self.conn.execute("SELECT * FROM validation_results WHERE run_id=%s ORDER BY id",
                                     (run_id,)).fetchall()
        return {"run": run, "invoice": inv, "lines": lines, "steps": steps, "findings": findings}

    def resolve(self, invoice_id: int, resolution: str, note: str) -> bool:
        r = self.conn.execute(
            """UPDATE invoices SET resolution=%s, resolution_note=%s, resolved_at=now()
               WHERE id=%s AND decision='REVIEW' RETURNING id""", (resolution, note, invoice_id)).fetchone()
        return r is not None

    def master_data(self) -> dict:
        vendors = self.conn.execute("SELECT id,name,tax_id,iban,currency FROM vendors ORDER BY name").fetchall()
        pos = self.conn.execute(
            """SELECT p.po_number, v.name AS vendor, p.currency, p.amount,
                      COALESCE((SELECT SUM(total) FROM invoices i WHERE i.po_number=p.po_number
                                AND COALESCE(i.resolution,i.decision)<>'REJECT' AND i.superseded_by IS NULL),0) AS billed
               FROM purchase_orders p JOIN vendors v ON v.id=p.vendor_id ORDER BY p.po_number""").fetchall()
        return {"vendors": vendors, "purchase_orders": pos}

    # ---- customisation: policy + master data
    def get_policy(self):
        from .policy import Policy
        r = self._one("SELECT version, settings FROM policy_versions ORDER BY version DESC LIMIT 1")
        return (Policy.model_validate(r["settings"]), r["version"]) if r else (Policy(), 0)

    def save_policy(self, policy, note: str = "") -> int:
        return self._one("INSERT INTO policy_versions (settings, note) VALUES (%s,%s) RETURNING version",
                         Jsonb(json.loads(policy.model_dump_json())), note)["version"]

    def policy_history(self) -> list[dict]:
        return self.conn.execute("SELECT version, note, created_at FROM policy_versions ORDER BY version DESC "
                                 "LIMIT 20").fetchall()

    def create_vendor(self, name, tax_id, iban, currency) -> int:
        try:
            return self._one("INSERT INTO vendors (name,tax_id,iban,currency) VALUES (%s,%s,%s,%s) RETURNING id",
                             name, tax_id, iban, currency)["id"]
        except psycopg.errors.UniqueViolation:
            raise ValueError(f"a vendor with tax id {tax_id} already exists")

    def create_po(self, po_number, vendor_id, amount) -> None:
        v = self._one("SELECT currency FROM vendors WHERE id=%s", vendor_id)
        if not v:
            raise ValueError("unknown vendor")
        try:
            self.conn.execute("INSERT INTO purchase_orders (po_number,vendor_id,currency,amount) VALUES (%s,%s,%s,%s)",
                              (po_number, vendor_id, v["currency"], amount))
        except psycopg.errors.UniqueViolation:
            raise ValueError(f"PO {po_number} already exists")

    def begin_rerun(self, run_id: int) -> dict | None:
        """Take the old result out of history while its document is processed again."""
        r = self._one("SELECT id, invoice_id, document_path, source_file FROM pipeline_runs "
                      "WHERE id=%s AND superseded_by IS NULL", run_id)
        if r and r["invoice_id"]:
            self.conn.execute("UPDATE invoices SET superseded_by=0 WHERE id=%s", (r["invoice_id"],))
        return r

    def finish_rerun(self, old_run_id: int, new_run_id: int | None) -> None:
        """Mark the old run as replaced (or restore it if the re-run crashed)."""
        with self.conn.transaction():
            if new_run_id:
                r = self._one("UPDATE pipeline_runs SET superseded_by=%s WHERE id=%s RETURNING invoice_id",
                              new_run_id, old_run_id)
                if r and r["invoice_id"]:
                    self.conn.execute("UPDATE invoices SET superseded_by=%s WHERE id=%s", (new_run_id, r["invoice_id"]))
            else:
                self.conn.execute("UPDATE invoices SET superseded_by=NULL WHERE id=(SELECT invoice_id FROM "
                                  "pipeline_runs WHERE id=%s)", (old_run_id,))

    def reset_invoices(self) -> None:
        self.conn.execute("TRUNCATE invoices, invoice_lines, pipeline_runs, pipeline_steps, validation_results "
                          "RESTART IDENTITY CASCADE")


# ------------------------------------------------------------------ in-memory (tests)
class MemoryStore:
    def __init__(self, vendors: list[Vendor] = (), pos: list[PurchaseOrder] = ()):
        self.vendors = list(vendors)
        self.pos = {p.po_number: p for p in pos}
        self.invoices: list[dict] = []

    def vendor_by_tax_id(self, tax_id):
        return next((v for v in self.vendors if v.tax_id and norm_id(v.tax_id) == norm_id(tax_id)), None)

    def all_vendors(self):
        return self.vendors

    def po(self, po_number):
        return self.pos.get(po_number)

    def _live(self):
        return [i for i in self.invoices if (i.get("resolution") or i["decision"]) != "REJECT"]

    def po_billed(self, po_number):
        return sum((i["total"] for i in self._live() if i["po_number"] == po_number), Decimal("0"))

    def invoice_by_sha(self, sha):
        return next((i for i in self.invoices if i["sha"] == sha), None)

    def invoice_by_number(self, vendor_id, number_norm):
        return next((i for i in self._live() if i["vendor_id"] == vendor_id and i["number_norm"] == number_norm), None)

    def similar_invoices(self, vendor_id, total, around, window_days):
        return [{k: i[k] for k in ("id", "invoice_number", "invoice_date", "total", "decision")}
                for i in self._live()
                if i["vendor_id"] == vendor_id and i["total"] == total and i["invoice_date"]
                and abs((i["invoice_date"] - around).days) <= window_days]

    def save(self, *, source_file, sha256, inv, vendor, tax_inclusive, decision, findings, steps,
             raw_extraction, model, document_path=None, policy=None, policy_version=None):
        inv_id = len(self.invoices) + 1
        self.invoices.append({"id": inv_id, "sha": sha256, "vendor_id": vendor.id if vendor else None,
                              "invoice_number": inv.invoice_number, "number_norm": inv.invoice_number_norm,
                              "invoice_date": inv.invoice_date, "total": inv.total, "po_number": inv.po_number,
                              "decision": decision.outcome})
        return inv_id, inv_id

    def save_failed(self, **kw) -> int:
        return 0

    def get_policy(self):
        from .policy import Policy
        return getattr(self, "policy", None) or Policy(), 0

    def vendor_invoice_count(self, vendor_id):
        return sum(1 for i in self.invoices if i["vendor_id"] == vendor_id)

    def open_pos(self, vendor_id):
        return [{"po_number": p.po_number, "amount": p.amount, "billed": self.po_billed(p.po_number)}
                for p in self.pos.values() if p.vendor_id == vendor_id]
