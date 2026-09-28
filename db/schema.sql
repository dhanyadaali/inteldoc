-- IntelDoc schema: master data + invoices + full processing trace.
-- Safe to re-run (idempotent).

-- ---------- Master data (what we validate against) ----------
CREATE TABLE IF NOT EXISTS vendors (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    tax_id      TEXT UNIQUE,
    iban        TEXT,                -- bank account on file; invoices must match
    currency    TEXT NOT NULL DEFAULT 'USD',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    po_number   TEXT PRIMARY KEY,
    vendor_id   INT NOT NULL REFERENCES vendors(id),
    currency    TEXT NOT NULL,
    amount      NUMERIC(14,2) NOT NULL,   -- total authorised spend
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------- Invoices (Load target) ----------
CREATE TABLE IF NOT EXISTS invoices (
    id                  SERIAL PRIMARY KEY,
    source_file         TEXT NOT NULL,
    file_sha256         TEXT NOT NULL,
    vendor_id           INT REFERENCES vendors(id),
    vendor_name         TEXT,
    invoice_number      TEXT,
    invoice_number_norm TEXT,             -- alphanumerics only, upper-case
    invoice_date        DATE,
    due_date            DATE,
    currency            TEXT,
    po_number           TEXT,
    iban                TEXT,
    subtotal            NUMERIC(14,2),
    tax                 NUMERIC(14,2),
    total               NUMERIC(14,2),
    tax_inclusive       BOOLEAN,
    decision            TEXT NOT NULL CHECK (decision IN ('APPROVE','REVIEW','REJECT')),
    -- human resolution of a REVIEW; the effective status is COALESCE(resolution, decision)
    resolution          TEXT CHECK (resolution IN ('APPROVE','REJECT')),
    resolution_note     TEXT,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_invoices_vendor_num ON invoices (vendor_id, invoice_number_norm);
CREATE INDEX IF NOT EXISTS ix_invoices_sha ON invoices (file_sha256);
CREATE INDEX IF NOT EXISTS ix_invoices_po ON invoices (po_number);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id          SERIAL PRIMARY KEY,
    invoice_id  INT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    line_no     INT NOT NULL,
    description TEXT,
    quantity    NUMERIC(14,4),
    unit_price  NUMERIC(14,4),
    amount      NUMERIC(14,2)
);

-- ---------- Trace: everything that happened in between ----------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           SERIAL PRIMARY KEY,
    invoice_id   INT REFERENCES invoices(id) ON DELETE CASCADE,
    source_file  TEXT NOT NULL,
    document_path TEXT,                 -- where the original file is kept (for preview)
    model        TEXT,
    decision     TEXT,                  -- APPROVE | REVIEW | REJECT | FAILED
    summary      TEXT,
    error        TEXT,
    raw_extraction JSONB,               -- exactly what the LLM returned
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS pipeline_steps (
    id          SERIAL PRIMARY KEY,
    run_id      INT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    seq         INT NOT NULL,
    stage       TEXT NOT NULL,          -- extract | transform | validate | decide | load
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ok | error
    detail      JSONB,
    duration_ms INT
);

CREATE TABLE IF NOT EXISTS validation_results (
    id        SERIAL PRIMARY KEY,
    run_id    INT NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
    rule      TEXT NOT NULL,
    outcome   TEXT NOT NULL CHECK (outcome IN ('pass','warn','fail','skip')),
    action    TEXT NOT NULL CHECK (action IN ('none','note','review','reject')),
    message   TEXT NOT NULL,
    evidence  JSONB
);

-- upgrades for databases created before these columns existed
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS document_path TEXT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS error TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS resolution TEXT CHECK (resolution IN ('APPROVE','REJECT'));
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS resolution_note TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

-- policy (customisation) - newest row is active; every run records the version it used
CREATE TABLE IF NOT EXISTS policy_versions (
    version     SERIAL PRIMARY KEY,
    settings    JSONB NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS policy_version INT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS policy JSONB;
-- a re-run replaces the earlier result for the same document; superseded rows leave all history checks
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS superseded_by INT;
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS superseded_by INT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS document_type TEXT;
