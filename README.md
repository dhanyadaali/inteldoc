# IntelDoc: invoice in, reasoned decision out

IntelDoc takes a real invoice (a PDF, a scan or a photo), extracts it with an LLM, validates it against
master data and history in Postgres, and returns **APPROVE / REVIEW / REJECT** with the reasons.
Every stage in between is visible, live in the browser as it runs, and stored for later.

> **Design rule: the LLM reads, the rules decide.** The model only copies what is printed.
> All arithmetic, matching and judgement is deterministic Python, so every decision can be explained,
> repeated and tested.


## Pipeline (ETL + decision)

```
 PDF / PNG / JPG
      │
 ┌────▼─────────────── EXTRACT ───────────────────────────────┐
 │ load_document  sha256, text layer?                         │
 │ ocr            local RapidOCR, rows rebuilt (scans only)   │
 │ llm_extraction gpt-oss-120b → JSON "as printed" + unsure   │
 └────┬───────────────────────────────────────────────────────┘
 ┌────▼─────────────── TRANSFORM ─────────────────────────────┐
 │ money "1.680,67 EUR" → 1680.67 · dates (ambiguity noted)   │
 │ invoice no. "INV 2026/0101" → INV20260101 · IBAN/tax id    │
 └────┬───────────────────────────────────────────────────────┘
 ┌────▼─────────────── VALIDATE (11 rules) ───────────────────┐
 │ document_type · required_fields · extraction_confidence ·  │
 │ arithmetic · dates · vendor_match · bank_details ·         │
 │ currency · duplicate · purchase_order · approval_limit     │
 │                         ← reads Postgres history + policy  │
 └────┬───────────────────────────────────────────────────────┘
 ┌────▼─── DECIDE ──┐   each rule asks for none / note / review / reject;
 │ strictest wins   │   the strictest request becomes the outcome
 └────┬─────────────┘   every check prints its working, e.g.
                        "25.00 + tax 4.50 = 29.50 · tax is 18.0% of net (printed 18%)"
 ┌────▼─── LOAD ────────────────────────────────────────────────┐
 │ invoices, invoice_lines, pipeline_runs, pipeline_steps,      │
 │ validation_results  (full trace, raw LLM output kept)        │
 └──────────────────────────────────────────────────────────────┘
```

## Edge cases

| # | Scenario | Why it's hard | What the process does |
|---|---|---|---|
| 1 | **Bank details swapped** (`07`) | Everything else on the invoice is perfect: known vendor, correct maths, plausible amount. This is how business-email-compromise fraud works. | The IBAN is compared with the one on file. On a mismatch the invoice is **held for review** with the instruction to confirm using contact details on file, never the ones on the invoice. |
| 2 | **Disguised duplicates** (`05`, `06`) | `INV 2026/0101` ≠ `INV-2026-0101` as strings. A resubmission under a *new* number has no matching number at all. | The number is normalised, so the same vendor with the same normalised number is **rejected**. The same vendor with the same amount within 30 days but a different number goes to **review**, and the message points at the original invoice. |
| 3 | **Cumulative PO overrun** (`02`→`04`) | Each partial invoice is fine on its own (2,400 < 10,000). Only the history shows that the third one takes the PO to 10,900. | The check is billed-so-far + this invoice against the PO (2% tolerance). Invoices awaiting review count toward billed-so-far. If a reviewer **rejects** one, its amount is released from the PO again. |
| 4 | **Tax-inclusive pricing vs a real error** (`08`, `09`) | German gross pricing means the lines add up to the *total*, not the net, so a naive "lines = subtotal" check wrongly flags it. Being lenient must not hide a real error. | Arithmetic is tried the tax-exclusive way first, then tax-inclusive, and the reading that fits is recorded. `08` is approved in inclusive mode. `09`, overstated by 100.00, fits neither reading and goes to review with the difference. |

Also handled:

- **Receipts, quotes and credit notes.** A receipt says the money is already gone, so paying it would pay
  twice. It is **rejected as "already paid"**, which you can change to review in Policy. A quote is rejected,
  and a credit note goes to review. This came from a real OpenAI receipt uploaded during testing.
- **Printed tax rate vs computed rate.** "18%" printed but tax equal to 20% of net goes to review.
- **Implied PO.** When no PO is quoted, the vendor's open POs with enough budget left are suggested.
  Policy can require a PO above an amount.
- **Tax-id labels.** "IN GST 9925…" and "VAT No: DE31…" are matched on the number itself.

Also handled: an **IBAN checksum and country-length check** on every invoice, which catches OCR misreads and
edited accounts before the master-data comparison. An ambiguous date like `03/04/2026` is resolved by policy and noted in the trace. European
number formats are parsed. If the model flags a money or identity field as unclear, the invoice goes to
review instead of guessing. A crashed run (unreadable file, model outage) is recorded as **FAILED** rather
than disappearing. Reviewers resolve REVIEW invoices in the UI, and that resolution feeds back into the
duplicate and PO checks.

## Measured results (real model: Groq `openai/gpt-oss-120b`)

- **Scenarios:** 9/9 decisions as expected. Each held or rejected invoice is stopped by exactly the rule its
  scenario targets. Text PDFs take about 1.2–2 s end to end.
- **Real scans** (26 Hugging Face invoices, OCR + LLM, `scripts/evaluate.py --limit 26`): 25/26 invoices
  had every field correct. The one misread (scan 021: an `O` read as `0` in the IBAN, one line item missed)
  was **caught by the pipeline**, which sent it to review with two reasons. The IBAN fails its checksum,
  and the lines fall 29.99 short of the subtotal.
- **Dataset label errors found:** 6 labels have IBANs that fail the mod-97 checksum or have the wrong
  length, 1 label total contradicts its own net + VAT (scan 013), and 1 label is missing the seller tax id.
  In each case the model's reading matches the printed page. The evaluator detects these instead of
  counting them against the model, and `seed.py` refuses to store an unverifiable IBAN in vendor master data.
- Scans take longer: about 7–15 s for OCR on CPU. Groq's free tier allows 8k tokens per minute, so long
  batches slow down while the SDK backs off and retries.

## Customisation (Policy tab) with guardrails

| Tunable | Range / choices | Default |
|---|---|---|
| Rounding tolerance | 0 – 5.00 | 0.02 |
| PO overrun tolerance | 0 – 10 % | 2 % |
| Resubmission look-back | 7 – 90 days | 30 |
| Flag invoices older than | 90 – 1,095 days | 365 |
| Auto-approval limit | 0 (off) – 10M | off |
| PO required above | 0 (off) – 10M | off |
| Ambiguous dates | month-first / day-first | month-first |
| Vendor not approved | review / reject | review |
| Same amount, new number | review / reject | review |
| Old invoice | note / review | note |
| Receipt / already paid | reject / review | reject |

**Locked, and cannot be relaxed:** a changed or invalid bank account always goes to review, an exact
duplicate is always rejected, totals that don't reconcile always go to review, and a missing number, date
or total always goes to review. The server enforces the bounds (a tolerance of 50 is refused). Each save
creates a new policy version, and every run records the version it was decided under.

Master data is editable in the UI too. Add a vendor (the IBAN must pass its checksum) or a PO. From a
run's detail you can **onboard an unknown vendor** straight from what was read, then **re-run**. The re-run
replaces the earlier result, so the document isn't treated as a duplicate of itself.

## Free-tier stack

| Piece | Choice | Cost |
|---|---|---|
| LLM | [Groq](https://console.groq.com) running **openai/gpt-oss-120b** (OpenAI's open-weight model) through the OpenAI SDK | free: ~1,000 requests/day, no card |
| OCR | RapidOCR (ONNX, runs locally on CPU) | free |
| Database | Postgres 16: Docker locally, [Neon](https://neon.tech) hosted | free: 0.5 GB |
| Hosting | [Render](https://render.com) free web service (`render.yaml`) | free |
| Data | [katanaml-org/invoices-donut-data-v1](https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1) (MIT, labelled scans) | free |

Any OpenAI-compatible endpoint works: set `OPENAI_BASE_URL`, `OPENAI_MODEL` and `OPENAI_API_KEY`. For
`gpt-4o-mini` on OpenAI or Gemini, also set `LLM_VISION=true` so page images are sent as well as text.
(GitHub Models was retired on 30 July 2026, so it is not an option.)

## Run it (everything in Docker)

Two containers: `db` (PostgreSQL 16) and `app` (web app + pipeline). Only Docker Desktop is needed.

```bash
cp .env.example .env                  # add GROQ_API_KEY (optional: DEMO_PASSWORD=...)
docker compose up -d --build          # first time ~3 min; then open http://localhost:8000
```

The app container seeds the tables, vendors and PO-7781 on start, and keeps existing history. Uploaded
documents are kept in the `uploads` volume, and the database in `pgdata`. Both survive restarts.

```bash
docker compose exec app python scripts/seed.py --reset          # clear history before a demo
docker compose exec app python -m pytest -q                     # 42 logic tests
docker compose exec app python scripts/process.py data/generated  # CLI: same pipeline, prints trace
docker compose exec app python scripts/evaluate.py --limit 10   # extraction accuracy vs ground truth
docker compose logs -f app                                      # live server log
docker compose stop                                             # pause both (data kept)
docker compose up -d --build                                    # after changing code
```

Without Docker for the app (Postgres still in Docker): `pip install -r requirements.txt`, then
`docker compose up -d db`, then `python scripts/seed.py`, then `uvicorn web.server:app --port 8000`.

## Demo script (about 5 minutes)

1. **Dashboard → Reset history.** Then **Process → Run all 9 in order.** Watch each stage light up. The
   queue shows outcomes landing and matching the "expect …" tags.
2. Open **07 bank details**: one red check with masked IBAN evidence; everything else passes.
3. Open **04 PO overrun**, then **Master data**: PO-7781 shows 10,900 / 10,000 in red.
   In the **Dashboard**, open run 04 → **Reject** with a note. Back in Master data it's 8,500 / 10,000.
4. Compare **08 vs 09**: the same vendor. One reconciles as tax-inclusive, the other is off by 100.00.
5. **Dataset scans → pick one**: the OCR step appears, and the LLM reads a real scan.
6. Drop a real PDF, such as an OpenAI receipt: it is rejected as "already paid", and the vendor is unknown.
   Open it, click **Add … as approved vendor**, then **Save & re-run**. The vendor now matches, and it is
   still correctly rejected because it's a receipt.
7. **Policy:** set the auto-approval limit to 1,000 with a note, then re-run 01. It goes to review under
   policy v1. Show that the locked controls can't be relaxed.
8. **Dashboard**: KPIs, amounts per currency (never summed across currencies), which rules stop
   invoices, and a full trace per run including the raw LLM output.

## Layout

```
intel_doc/   extract.py  ocr.py  transform.py  validate.py  decide.py  pipeline.py  store.py  models.py  config.py
web/         server.py (FastAPI + SSE, one ordered worker)   static/ (vanilla JS UI, no build)
scripts/     generate_invoices.py  fetch_dataset.py  seed.py  process.py  evaluate.py
db/          schema.sql
tests/       test_scenarios.py
```
