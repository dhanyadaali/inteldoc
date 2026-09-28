"""TRANSFORM: strings-as-printed -> typed, normalised values.

Every non-obvious interpretation (an ambiguous date, a guessed currency) is
recorded as a note so it shows up in the trace instead of being silently assumed.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import config
from .models import Invoice, Line, RawExtraction

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
ISO_CODES = {"USD", "EUR", "GBP", "INR", "JPY", "CAD", "AUD", "CHF", "SGD", "AED"}

DATE_FORMATS_UNAMBIGUOUS = ["%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y",
                            "%d-%b-%Y", "%d.%m.%Y"]


def parse_money(s: str | None) -> Decimal | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")") or "-" in s
    digits = re.sub(r"[^\d.,]", "", s)
    if not digits:
        return None
    if "," in digits and "." in digits:
        # whichever separator comes last is the decimal point
        if digits.rfind(",") > digits.rfind("."):
            digits = digits.replace(".", "").replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif "," in digits:
        # "1,234" / "1,234,567" -> thousands;  "12,50" -> decimal comma
        if re.fullmatch(r"\d{1,3}(,\d{3})+", digits):
            digits = digits.replace(",", "")
        else:
            digits = digits.replace(",", ".")
    elif digits.count(".") > 1:
        digits = digits.replace(".", "")  # "1.234.567"
    try:
        value = Decimal(digits)
    except InvalidOperation:
        return None
    return -value if negative else value


def parse_date(s: str | None, notes: list[str], field: str, order: str | None = None) -> date | None:
    if not s:
        return None
    s = s.strip().rstrip(".")
    for fmt in DATE_FORMATS_UNAMBIGUOUS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if not m:
        notes.append(f"{field}: could not parse date '{s}'")
        return None
    a, b, y = int(m[1]), int(m[2]), int(m[3])
    y = y + 2000 if y < 100 else y
    mdy, dmy = _safe_date(y, a, b), _safe_date(y, b, a)
    if mdy and dmy and mdy != dmy:
        order = order or config.DATE_ORDER
        chosen = mdy if order == "MDY" else dmy
        notes.append(f"{field}: '{s}' is ambiguous (MDY={mdy}, DMY={dmy}); policy says {order} -> {chosen}")
        return chosen
    if not (mdy or dmy):
        notes.append(f"{field}: '{s}' is not a valid date")
    return mdy or dmy


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def normalise_invoice_number(s: str | None) -> str | None:
    """'INV-2024/0042' and 'inv 20240042' both become 'INV20240042'."""
    if not s:
        return None
    return re.sub(r"[^A-Za-z0-9]", "", s).upper() or None


def norm_id(s: str | None) -> str:
    """Tax ids / IBANs compared without spaces, dashes or case."""
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()


# Official IBAN lengths for common countries (ISO 13616 registry); others fall back to 15-34.
IBAN_LENGTHS = {"GB": 22, "DE": 22, "IE": 22, "FR": 27, "IT": 27, "ES": 24, "NL": 18, "BE": 16, "AT": 20,
                "CH": 21, "PT": 25, "PL": 28, "SE": 24, "DK": 18, "NO": 15, "FI": 18, "LU": 20, "AE": 23}


def iban_valid(iban: str | None) -> bool:
    """ISO 13616 length + mod-97 check. Catches misreads (5 vs S, dropped or doubled digits)."""
    s = norm_id(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    if len(s) != IBAN_LENGTHS.get(s[:2], len(s)):
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


_TAX_LABEL = re.compile(r"^(?:[A-Z]{2}\s+)?(?:VAT|GSTIN|GST|TIN|EIN|ABN|TAX\s*ID|TAX\s*NO)\b[\s:#.\-]*(?:NO\.?|NUMBER)?[\s:#.\-]*",
                        re.IGNORECASE)


def clean_tax_id(s: str | None) -> str | None:
    """'IN GST 9925USA29095OS3' / 'VAT No: DE318877465' -> the number itself; 'DE318877465' is kept whole."""
    if not s:
        return None
    return _TAX_LABEL.sub("", s.strip()).strip() or None


def normalise_iban(s: str | None) -> str | None:
    if not s:
        return None
    return re.sub(r"\s", "", s).upper() or None


def detect_currency(raw: RawExtraction, notes: list[str]) -> str | None:
    if raw.currency:
        code = raw.currency.strip().upper()
        if code in ISO_CODES:
            return code
        for sym, iso in CURRENCY_SYMBOLS.items():
            if sym in raw.currency:
                return iso
    # fall back to symbols printed next to amounts
    for value in (raw.total, raw.subtotal, *(l.amount for l in raw.line_items)):
        if not value:
            continue
        code = next((c for c in ISO_CODES if re.search(rf"\b{c}\b", value.upper())), None)
        if code:
            notes.append(f"currency inferred from code printed with amounts -> {code}")
            return code
        for sym, iso in CURRENCY_SYMBOLS.items():
            if sym in value:
                notes.append(f"currency inferred from symbol '{sym}' -> {iso}")
                return iso
    return None


def transform(raw: RawExtraction, date_order: str | None = None) -> tuple[Invoice, list[str]]:
    notes: list[str] = []
    lines = [
        Line(
            description=(l.description or "").strip() or None,
            quantity=parse_money(l.quantity),
            unit_price=parse_money(l.unit_price),
            amount=parse_money(l.amount),
        )
        for l in raw.line_items
    ]
    inv = Invoice(
        vendor_name=(raw.vendor_name or "").strip() or None,
        vendor_tax_id=clean_tax_id(raw.vendor_tax_id),
        vendor_iban=normalise_iban(raw.vendor_iban),
        invoice_number=(raw.invoice_number or "").strip() or None,
        invoice_number_norm=normalise_invoice_number(raw.invoice_number),
        invoice_date=parse_date(raw.invoice_date, notes, "invoice_date", date_order),
        due_date=parse_date(raw.due_date, notes, "due_date", date_order),
        currency=detect_currency(raw, notes),
        po_number=(raw.po_number or "").strip().upper() or None,
        lines=lines,
        subtotal=parse_money(raw.subtotal),
        tax=parse_money(raw.tax),
        total=parse_money(raw.total),
        tax_rate=parse_money(raw.tax_rate),
        amount_paid=parse_money(raw.amount_paid),
        balance_due=parse_money(raw.balance_due),
        document_type=raw.document_type,
        tax_inclusive_hint=raw.tax_inclusive_hint,
        unclear_fields=raw.unclear_fields,
    )
    return inv, notes
