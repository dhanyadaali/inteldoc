"""EXTRACT: turn a PDF / image into a RawExtraction using an OpenAI-compatible model.

Text comes from the PDF's text layer, or from local OCR for scans/photos.
Vision models (LLM_VISION=true) additionally get the page images.

The model is told to *read*, not to *compute*: every value is copied as printed.
All arithmetic and judgement happens later in deterministic code.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from openai import OpenAI
from pydantic import ValidationError

from . import config
from .models import RawExtraction

MAX_PAGES = 3

SYSTEM_PROMPT = """You are an accounts-payable data-entry clerk. Read the document and return ONE JSON object.

Rules:
- Copy every value EXACTLY as printed (keep currency symbols, separators, date format). Do not calculate, convert or fill in values that are not printed.
- If a field is not on the document use null. Never invent a PO number, tax id or bank account.
- document_type: "invoice" (a request for payment), "receipt" (confirms a payment already made, e.g. "Receipt", "Paid", "Amount paid" with nothing due), "credit_note" (credit memo / refund), "quote" (quotation, estimate, pro-forma, order confirmation), otherwise "other".
- invoice_number: the invoice number; for a receipt use the receipt/invoice number printed.
- invoice_date: the issue date. If only a "date paid" / "receipt date" is printed, use that.
- vendor_tax_id: the SELLER's tax registration (VAT, GST, EIN, TIN, ABN...). Keep the number only as printed.
- line_items: one entry per billed line. "amount" is the line total as printed; if the table shows both net and gross line totals, use the NET one.
- subtotal = total before tax (net), tax = total tax/VAT amount in the invoice currency, total = invoice total (gross).
- tax_rate = the tax percentage printed (e.g. "18%"), null if none or several different rates.
- amount_paid = amount already paid if printed; balance_due = amount still due if printed.
- tax_inclusive_hint = true only if the page states that prices INCLUDE tax/VAT (e.g. "incl. VAT", "tax included"); false if it states they exclude it; otherwise null.
- unclear_fields: names of fields you could not read confidently (blurred, cut off, handwritten, ambiguous).

JSON keys:
{"document_type","vendor_name","vendor_tax_id","vendor_iban","invoice_number","invoice_date","due_date","currency","po_number",
 "line_items":[{"description","quantity","unit_price","amount"}],
 "subtotal","tax","tax_rate","total","amount_paid","balance_due","tax_inclusive_hint","unclear_fields"}
The vendor is the SELLER issuing the invoice, not the client."""


@dataclass
class Document:
    path: Path
    sha256: str
    images_b64: list[str]  # PNG pages
    text: str               # embedded text layer ("" for scans / images)
    ocr_text: str = ""
    ocr_confidence: float | None = None

    @property
    def needs_ocr(self) -> bool:
        return len(self.text) < 50  # no usable text layer

    def run_ocr(self) -> None:
        from .ocr import ocr_png
        pages = [ocr_png(base64.b64decode(img)) for img in self.images_b64]
        self.ocr_text = "\n\n".join(t for t, _ in pages)
        self.ocr_confidence = round(min(c for _, c in pages), 3) if pages else 0.0


def load_document(path: str | Path) -> Document:
    path = Path(path)
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    if path.suffix.lower() == ".pdf":
        pdf = fitz.open(stream=data, filetype="pdf")
        pages = list(pdf)[:MAX_PAGES]
        text = "\n".join(p.get_text() for p in pages).strip()
        images = [base64.b64encode(p.get_pixmap(dpi=150).tobytes("png")).decode() for p in pages]
        return Document(path, sha, images, text)

    # Images: normalise to PNG through PyMuPDF so any common format works.
    pix = fitz.Pixmap(data)
    if pix.alpha or pix.n > 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return Document(path, sha, [base64.b64encode(pix.tobytes("png")).decode()], "")


class Extractor:
    def __init__(self, model: str | None = None):
        if not config.OPENAI_API_KEY:
            raise RuntimeError("No API key. Set GROQ_API_KEY (free) or OPENAI_API_KEY in .env")
        self.model = model or config.OPENAI_MODEL
        # max_retries: the SDK backs off on 429s, which free tiers hit under bursts
        self.client = OpenAI(base_url=config.OPENAI_BASE_URL, api_key=config.OPENAI_API_KEY, max_retries=5)

    def extract(self, doc: Document) -> tuple[RawExtraction, dict]:
        """Returns the parsed extraction plus metadata (tokens, attempts) for the trace."""
        content: list[dict] = [{"type": "text", "text": "Extract this invoice."}]
        if doc.text and not doc.needs_ocr:
            content.append({"type": "text", "text": f"Document text:\n{doc.text[:12000]}"})
        if doc.ocr_text:
            content.append({"type": "text", "text": "OCR text of a scanned page (rows reconstructed, cells "
                            f"separated by ' | '; OCR may drop spaces):\n{doc.ocr_text[:12000]}"})
        if config.LLM_VISION:
            for img in doc.images_b64:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img}"}})
        extra = {"reasoning_effort": "low"} if "gpt-oss" in self.model else {}

        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]
        usage = {"prompt_tokens": 0, "completion_tokens": 0}

        # One retry: if the JSON doesn't fit the schema, show the model the error.
        for attempt in (1, 2):
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
                **({"extra_body": extra} if extra else {}),
            )
            if resp.usage:
                usage["prompt_tokens"] += resp.usage.prompt_tokens
                usage["completion_tokens"] += resp.usage.completion_tokens
            raw = resp.choices[0].message.content or "{}"
            try:
                data = _stringify(json.loads(raw))
                if data.get("document_type") not in ("invoice", "receipt", "credit_note", "quote", "other"):
                    data["document_type"] = "other" if data.get("document_type") else "invoice"
                parsed = RawExtraction.model_validate(data)
                return parsed, {"model": self.model, "attempts": attempt, **usage}
            except (json.JSONDecodeError, ValidationError) as e:
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"That JSON was invalid: {e}. Return corrected JSON only."},
                ]
        raise RuntimeError("Model did not return valid JSON after 2 attempts")


def _stringify(obj):
    """Models sometimes return numbers as numbers; we want 'as printed' strings."""
    if isinstance(obj, dict):
        return {k: v if isinstance(v, bool) or k in ("unclear_fields", "document_type") else _stringify(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify(v) for v in obj]
    if isinstance(obj, (int, float)):
        return str(obj)
    return obj
