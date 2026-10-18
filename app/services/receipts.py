"""Reading receipts: merchant, date, total and line items from a photo or PDF.

The method is chosen with RECEIPT_SCANNER:
  off        – disabled (default)
  tesseract  – local text recognition in the container, plus rules to find total/date/items
  claude     – Anthropic's Claude reads the image/PDF and returns structured JSON (the file leaves your server)
  ollama     – a local vision model served by Ollama returns the same JSON
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.services import csv_parser as cp
from app.services.rules import first_match, load_rules

log = logging.getLogger(__name__)

PROVIDERS = {
    "tesseract": "Local text recognition (Tesseract)",
    "claude": "Claude (Anthropic API)",
    "ollama": "Local AI model (Ollama)",
}


class ReceiptScanError(Exception):
    """A scan failed; the message is shown to the user."""


@dataclass
class ReceiptItem:
    text: str
    amount_cents: int  # positive = cost, negative = discount / deposit return


@dataclass
class ReceiptData:
    merchant: str = ""
    date: date | None = None
    total_cents: int | None = None
    currency: str = "EUR"
    items: list[ReceiptItem] = field(default_factory=list)
    provider: str = ""


def provider() -> str:
    name = (get_settings().receipt_scanner or "off").strip().lower()
    return name if name in PROVIDERS else "off"


def scan(content: bytes, content_type: str) -> ReceiptData:
    name = provider()
    if name == "off":
        raise ReceiptScanError("Reading receipts is turned off on this server (RECEIPT_SCANNER).")
    reader = {"tesseract": _scan_tesseract, "claude": _scan_claude, "ollama": _scan_ollama}[name]
    data = reader(content, content_type)
    data.provider = name
    return data


# ---------------------------------------------------------------- shared helpers


def _cents(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
        return cp.parse_amount(str(value))
    except (InvalidOperation, cp.ParseError, ValueError):
        return None


def _date(value) -> date | None:
    if not value:
        return None
    try:
        return cp.parse_date(str(value))
    except cp.ParseError:
        return None


def from_json(data: dict) -> ReceiptData:
    """Normalize the JSON returned by an AI model (amounts in currency units)."""
    items = []
    for it in data.get("items") or []:
        cents = _cents(it.get("amount"))
        text = str(it.get("description") or "").strip()
        if cents and text:
            items.append(ReceiptItem(text[:200], cents))
    return ReceiptData(
        merchant=str(data.get("merchant") or "").strip()[:200],
        date=_date(data.get("date")),
        total_cents=_cents(data.get("total")),
        currency=(str(data.get("currency") or "EUR").strip().upper() or "EUR")[:3],
        items=items,
    )


def pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:10])
    except Exception as exc:  # malformed PDFs raise many different errors
        raise ReceiptScanError("The PDF could not be read.") from exc


# ---------------------------------------------------------------- text heuristics (Tesseract, PDF text)

_PRICE = r"-?\d{1,5}(?:[.,]\d{3})*[.,]\d{2}-?"
_LINE_PRICE = re.compile(rf"({_PRICE})\s*(?:€|EUR)?\s*(?:[A-D]|\*|[A-D]\s*\*)?\s*$", re.IGNORECASE)
_DATE = re.compile(r"\b(\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2}))\b|\b(\d{4}-\d{2}-\d{2})\b")
_TOTAL_WORDS = ("zu zahlen", "summe", "gesamt", "total", "endbetrag", "rechnungsbetrag", "betrag eur", "amount due")
_NOT_ITEMS = (
    "summe", "gesamt", "total", "zu zahlen", "endbetrag", "mwst", "ust", "steuer", "netto", "brutto",
    "bar", "rückgeld", "rueckgeld", "gegeben", "ec-karte", "ec karte", "girocard", "kartenzahlung", "karte",
    "visa", "mastercard", "zwischensumme", "betrag", "change", "tax", "vat", "subtotal", "trinkgeld",
)


def parse_text(text: str) -> ReceiptData:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    result = ReceiptData()

    for ln in lines:
        m = _DATE.search(ln)
        if m:
            result.date = _date(m.group(1) or m.group(2))
            if result.date:
                break

    for ln in lines[:8]:
        letters = sum(ch.isalpha() for ch in ln)
        if letters >= 3 and not _LINE_PRICE.search(ln) and not _DATE.search(ln):
            result.merchant = ln[:200]
            break

    totals: list[int] = []
    for ln in lines:
        low = ln.lower()
        m = _LINE_PRICE.search(ln)
        if not m:
            continue
        cents = _cents(m.group(1))
        if cents is None:
            continue
        if any(w in low for w in _TOTAL_WORDS) and "zwischensumme" not in low:
            totals.append(abs(cents))
            continue
        if any(w in low for w in _NOT_ITEMS):
            continue
        label = ln[: m.start()].strip(" .:*-")
        if sum(ch.isalpha() for ch in label) < 2:
            continue
        result.items.append(ReceiptItem(label[:200], cents))
    if totals:
        result.total_cents = max(totals)
    elif result.items:
        result.total_cents = sum(i.amount_cents for i in result.items)
    return result


def _scan_tesseract(content: bytes, content_type: str) -> ReceiptData:
    if content_type == "application/pdf":
        text = pdf_text(content)
        if not text.strip():
            raise ReceiptScanError("This PDF has no text layer. Take a photo of the receipt instead.")
        return parse_text(text)
    try:
        import pytesseract
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(content)) as img:
            gray = ImageOps.autocontrast(ImageOps.grayscale(img))
            text = pytesseract.image_to_string(
                gray, lang=get_settings().tesseract_lang, config="--psm 4", timeout=get_settings().receipt_scan_timeout
            )
    except ImportError as exc:
        raise ReceiptScanError("Tesseract is not available in this installation.") from exc
    except RuntimeError as exc:  # pytesseract timeout
        raise ReceiptScanError("Reading the receipt took too long.") from exc
    except Exception as exc:  # TesseractNotFoundError, TesseractError
        log.exception("Tesseract failed")
        raise ReceiptScanError(f"Text recognition failed: {exc.__class__.__name__}") from exc
    return parse_text(text)


# ---------------------------------------------------------------- AI models (Claude, Ollama)

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": "string", "description": "Shop or company name, empty if unknown"},
        "date": {"type": "string", "description": "Purchase date as YYYY-MM-DD, empty if unknown"},
        "total": {"type": "number", "description": "Amount actually paid, in the receipt currency"},
        "currency": {"type": "string", "description": "ISO 4217 code, e.g. EUR"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "amount": {"type": "number", "description": "Line total; negative for discounts and deposit returns"},
                },
                "required": ["description", "amount"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["merchant", "date", "total", "currency", "items"],
    "additionalProperties": False,
}

PROMPT = (
    "Read this receipt or invoice and return its data as JSON.\n"
    "- merchant: the shop or company that issued it.\n"
    "- date: the purchase or invoice date as YYYY-MM-DD.\n"
    "- total: the amount actually paid (after discounts, including tax).\n"
    "- items: one entry per purchased line with its line total (quantity x price). Use negative amounts for "
    "discounts, coupons and deposit returns. Leave out subtotals, tax lines, payment and change lines.\n"
    "If something cannot be read, use an empty string, 0 or an empty list rather than guessing."
)


def _scan_claude(content: bytes, content_type: str) -> ReceiptData:
    import anthropic

    s = get_settings()
    b64 = base64.standard_b64encode(content).decode()
    if content_type == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": b64}}
    client = anthropic.Anthropic(api_key=s.anthropic_api_key or None, timeout=s.receipt_scan_timeout, max_retries=2)
    try:
        response = client.beta.messages.create(
            model=s.receipt_claude_model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if the model declines, the API retries on Anthropic's recommended fallback
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": RECEIPT_SCHEMA}},
            messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
        )
    except anthropic.AuthenticationError as exc:
        raise ReceiptScanError("The Anthropic API key is missing or invalid (ANTHROPIC_API_KEY).") from exc
    except anthropic.RateLimitError as exc:
        raise ReceiptScanError("The Anthropic API is rate limited right now. Please try again in a minute.") from exc
    except anthropic.APIStatusError as exc:
        log.warning("Claude receipt scan failed: %s %s", exc.status_code, exc.message)
        raise ReceiptScanError(f"Claude could not read the receipt (error {exc.status_code}).") from exc
    except anthropic.APIConnectionError as exc:
        raise ReceiptScanError("The Anthropic API could not be reached.") from exc
    if response.stop_reason == "refusal":
        raise ReceiptScanError("Claude declined to read this file.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return from_json(json.loads(text))
    except (json.JSONDecodeError, AttributeError) as exc:
        raise ReceiptScanError("Claude returned an unreadable answer.") from exc


def ollama_request(payload: dict) -> dict:
    """POST to the Ollama chat API (patched in tests)."""
    s = get_settings()
    req = urllib.request.Request(
        s.ollama_url.rstrip("/") + "/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=s.receipt_scan_timeout) as resp:
        return json.loads(resp.read().decode())


def _scan_ollama(content: bytes, content_type: str) -> ReceiptData:
    s = get_settings()
    message: dict = {"role": "user", "content": PROMPT}
    if content_type == "application/pdf":
        text = pdf_text(content)
        if not text.strip():
            raise ReceiptScanError("This PDF has no text layer. Take a photo of the receipt instead.")
        message["content"] = PROMPT + "\n\nReceipt text:\n" + text[:20000]
    else:
        message["images"] = [base64.standard_b64encode(content).decode()]
    try:
        reply = ollama_request({
            "model": s.ollama_model, "messages": [message], "format": RECEIPT_SCHEMA,
            "stream": False, "options": {"temperature": 0},
        })
        return from_json(json.loads(reply["message"]["content"]))
    except urllib.error.HTTPError as exc:
        raise ReceiptScanError(f"Ollama answered with error {exc.code}. Is the model “{s.ollama_model}” installed?") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReceiptScanError(f"Ollama could not be reached at {s.ollama_url}.") from exc
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        raise ReceiptScanError("The local AI model returned an unreadable answer.") from exc


# ---------------------------------------------------------------- matching & suggestions


def candidates(db: Session, user_id: int, data: ReceiptData, limit: int = 5) -> list[models.Transaction]:
    """Expense transactions with exactly the receipt total, closest date first."""
    if not data.total_cents:
        return []
    T = models.Transaction
    stmt = select(T).where(T.user_id == user_id, T.amount_cents == -abs(data.total_cents))
    if data.date:
        stmt = stmt.where(T.booking_date >= data.date - timedelta(days=3), T.booking_date <= data.date + timedelta(days=10))
    else:
        stmt = stmt.where(T.booking_date >= date.today() - timedelta(days=60))
    txs = list(db.scalars(stmt).unique())
    ref = data.date or date.today()
    txs.sort(key=lambda t: (abs((t.booking_date - ref).days), -t.id))
    return txs[:limit]


def suggested_parts(db: Session, user_id: int, data: ReceiptData) -> list[dict]:
    """Receipt items as split parts (signed like an expense) with categories from the user's rules.

    Split parts all share the transaction's sign, so discounts and deposit returns are folded
    into the item before them (or the largest item if that would not stay a cost).
    """
    rules = load_rules(db, user_id)
    parts: list[dict] = []
    credits: list[ReceiptItem] = []
    for it in data.items:
        if it.amount_cents < 0:
            if parts and -parts[-1]["amount_cents"] > -it.amount_cents:
                parts[-1]["amount_cents"] -= it.amount_cents  # e.g. -49.00 + 8.00 = -41.00
                parts[-1]["note"] = f"{parts[-1]['note']} (incl. {it.text})"[:255]
            else:
                credits.append(it)
            continue
        amount = -it.amount_cents  # receipt costs are expenses
        rule = first_match(rules, it.text, it.text, "", amount)
        parts.append({"amount_cents": amount, "note": it.text, "category_id": rule.category_id if rule else None})
    for it in credits:
        if not parts:
            break
        largest = min(parts, key=lambda p: p["amount_cents"])
        if -largest["amount_cents"] > -it.amount_cents:
            largest["amount_cents"] -= it.amount_cents
            largest["note"] = f"{largest['note']} (incl. {it.text})"[:255]
    return parts


def to_dict(data: ReceiptData) -> dict:
    return {
        "merchant": data.merchant,
        "date": data.date.isoformat() if data.date else None,
        "total_cents": data.total_cents,
        "currency": data.currency,
        "items": [{"text": i.text, "amount_cents": i.amount_cents} for i in data.items],
        "provider": data.provider,
    }
