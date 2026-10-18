import io
import json
from datetime import date
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import get_settings
from app.services import receipts

REWE_TEXT = """
REWE Markt GmbH
Hauptstr. 1, 12345 Berlin
UID Nr.: DE123456789
BIO BANANEN              1,99 B
VOLLMILCH 3,5%           1,19 B
2 x 0,89
BROT                     1,78 B
PFAND RUECKGABE         -0,75 B
--------------------------------
SUMME            EUR     4,21
Geg. EC-Karte    EUR     4,21
MwSt 7%   Netto 3,93   0,28
Datum: 03.05.2025  Uhrzeit 10:12
"""


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (600, 900), "white").save(out, "JPEG")
    return out.getvalue()


def _blank_pdf() -> bytes:
    """A valid PDF without a text layer, like a scanned receipt."""
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=300, height=500)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


PDF = _blank_pdf()
AI_ANSWER = {
    "merchant": "Baumarkt Müller", "date": "2025-05-03", "total": 45.99, "currency": "eur",
    "items": [{"description": "Schrauben", "amount": 4.99}, {"description": "Bohrmaschine", "amount": 49.0},
              {"description": "Rabatt", "amount": -8.0}, {"description": "", "amount": 1.0}],
}


def test_parse_text_german_receipt():
    r = receipts.parse_text(REWE_TEXT)
    assert r.merchant == "REWE Markt GmbH"
    assert r.date == date(2025, 5, 3)
    assert r.total_cents == 421
    assert [(i.text, i.amount_cents) for i in r.items] == [
        ("BIO BANANEN", 199), ("VOLLMILCH 3,5%", 119), ("BROT", 178), ("PFAND RUECKGABE", -75)]


def test_parse_text_without_total_sums_items():
    r = receipts.parse_text("Kiosk am Eck\nCola 2,50\nChips 1,99\n")
    assert r.total_cents == 449 and r.date is None


def test_from_json_normalizes():
    r = receipts.from_json(AI_ANSWER)
    assert (r.merchant, r.date, r.total_cents, r.currency) == ("Baumarkt Müller", date(2025, 5, 3), 4599, "EUR")
    assert [(i.text, i.amount_cents) for i in r.items] == [("Schrauben", 499), ("Bohrmaschine", 4900), ("Rabatt", -800)]


def test_off_by_default():
    assert receipts.provider() == "off"
    with pytest.raises(receipts.ReceiptScanError):
        receipts.scan(_jpeg(), "image/jpeg")


@pytest.fixture
def scanner(monkeypatch):
    def use(name, **extra):
        monkeypatch.setattr(get_settings(), "receipt_scanner", name)
        for k, v in extra.items():
            monkeypatch.setattr(get_settings(), k, v)
    return use


def test_claude_request_shape(scanner, monkeypatch):
    import anthropic

    scanner("claude", receipt_claude_model="claude-opus-5")
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(AI_ANSWER))])

    class FakeClient:
        def __init__(self, **kw):
            self.beta = SimpleNamespace(messages=FakeMessages())

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    r = receipts.scan(_jpeg(), "image/jpeg")
    assert r.total_cents == 4599 and r.provider == "claude"
    kw = calls[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["format"]["schema"] == receipts.RECEIPT_SCHEMA
    image = kw["messages"][0]["content"][0]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"

    receipts.scan(PDF, "application/pdf")
    assert calls[1]["messages"][0]["content"][0]["type"] == "document"

    class Refusing(FakeMessages):
        def create(self, **kw):
            return SimpleNamespace(stop_reason="refusal", content=[])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: SimpleNamespace(beta=SimpleNamespace(messages=Refusing())))
    with pytest.raises(receipts.ReceiptScanError, match="declined"):
        receipts.scan(_jpeg(), "image/jpeg")


def test_ollama_request_shape(scanner, monkeypatch):
    scanner("ollama", ollama_model="qwen2.5vl")
    sent = []
    monkeypatch.setattr(receipts, "ollama_request", lambda payload: sent.append(payload) or {"message": {"content": json.dumps(AI_ANSWER)}})
    r = receipts.scan(_jpeg(), "image/jpeg")
    assert r.merchant == "Baumarkt Müller"
    p = sent[0]
    assert p["model"] == "qwen2.5vl" and p["format"] == receipts.RECEIPT_SCHEMA and p["stream"] is False
    assert p["messages"][0]["images"] and "Read this receipt" in p["messages"][0]["content"]

    def unreachable(payload):
        raise OSError("connection refused")
    monkeypatch.setattr(receipts, "ollama_request", unreachable)
    with pytest.raises(receipts.ReceiptScanError, match="could not be reached"):
        receipts.scan(_jpeg(), "image/jpeg")


def test_tesseract_path(scanner, monkeypatch):
    import pytesseract

    scanner("tesseract")
    seen = {}

    def fake_ocr(img, lang, config, timeout):
        seen.update(mode=img.mode, lang=lang)
        return REWE_TEXT
    monkeypatch.setattr(pytesseract, "image_to_string", fake_ocr)
    r = receipts.scan(_jpeg(), "image/jpeg")
    assert r.total_cents == 421 and seen == {"mode": "L", "lang": "deu+eng"}
    with pytest.raises(receipts.ReceiptScanError, match="no text layer"):
        receipts.scan(PDF, "application/pdf")


def test_scan_match_attach_flow(admin, scanner, monkeypatch):
    scanner("ollama")
    monkeypatch.setattr(receipts, "ollama_request", lambda payload: {"message": {"content": json.dumps(AI_ANSWER)}})
    assert admin.get("/api/receipts/config").json() == {
        "enabled": True, "provider": "ollama", "label": "Local AI model (Ollama)", "leaves_server": False}

    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    admin.post("/api/rules", json={"field": "description", "pattern": "bohr", "category_id": cats["Shopping"]})
    match = admin.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-05-05",
                                                  "amount_cents": -4599, "description": "KARTENZAHLUNG BAUMARKT"}).json()
    admin.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-04-01",
                                          "amount_cents": -4599, "description": "too early"})

    r = admin.post("/api/receipts/scan", files={"file": ("bon.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 200, r.text
    scan = r.json()
    assert scan["receipt"]["total_cents"] == 4599
    assert [c["id"] for c in scan["candidates"]] == [match["id"]]
    # The discount is folded into the item before it, so the parts add up to the total.
    assert [(p["note"], p["amount_cents"], p["category_id"]) for p in scan["parts"]] == [
        ("Schrauben", -499, None), ("Bohrmaschine (incl. Rabatt)", -4100, cats["Shopping"])]
    assert sum(p["amount_cents"] for p in scan["parts"]) == -4599

    res = admin.post(f"/api/receipts/{scan['token']}/attach", json={"transaction_id": match["id"]}).json()
    assert res["transaction"]["attachments"][0]["filename"] == "bon.jpg"
    assert admin.post(f"/api/receipts/{scan['token']}/attach", json={"transaction_id": match["id"]}).status_code == 404

    # Without a matching transaction: create one from the receipt.
    scan2 = admin.post("/api/receipts/scan", files={"file": ("rechnung.pdf", PDF, "application/pdf")})
    assert scan2.status_code == 422  # the fake PDF has no text layer for Ollama
    scan2 = admin.post("/api/receipts/scan", files={"file": ("bon2.jpg", _jpeg(), "image/jpeg")}).json()
    created = admin.post(f"/api/receipts/{scan2['token']}/attach", json={"create": {
        "account_id": acc["id"], "booking_date": "2025-05-03", "amount_cents": -4599, "description": "Baumarkt Müller"}}).json()
    assert created["transaction"]["description"] == "Baumarkt Müller" and len(created["transaction"]["attachments"]) == 1

    # Reading an already attached receipt.
    att_id = res["attachment"]["id"]
    again = admin.post(f"/api/attachments/{att_id}/scan").json()
    assert again["receipt"]["merchant"] == "Baumarkt Müller" and len(again["parts"]) == 2


def test_scan_disabled_and_isolated(admin, client):
    assert admin.get("/api/receipts/config").json()["enabled"] is False
    r = admin.post("/api/receipts/scan", files={"file": ("bon.jpg", _jpeg(), "image/jpeg")})
    assert r.status_code == 422 and "turned off" in r.json()["detail"]
    assert admin.post("/api/receipts/unknown-token/attach", json={"transaction_id": 1}).status_code == 404


def test_credit_before_any_item_goes_to_largest(db):
    data = receipts.ReceiptData(items=[receipts.ReceiptItem("Coupon", -100), receipts.ReceiptItem("A", 300),
                                       receipts.ReceiptItem("B", 900), receipts.ReceiptItem("Pfand", -25)])
    parts = receipts.suggested_parts(db, 1, data)
    assert [(p["note"], p["amount_cents"]) for p in parts] == [("A", -300), ("B (incl. Pfand) (incl. Coupon)", -775)]
