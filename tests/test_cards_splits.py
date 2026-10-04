from fastapi.testclient import TestClient


def _import(c: TestClient, account_id: int, csv: str) -> dict:
    p = c.post("/api/imports/preview", files={"file": ("x.csv", csv.encode(), "text/csv")}, data={"account_id": account_id}).json()
    return c.post("/api/imports/commit", json={"token": p["token"], "account_id": account_id, "mapping": p["mapping"]}).json()


GIRO_CSV = (
    "Buchungstag;Verwendungszweck;Beguenstigter/Zahlungspflichtiger;Betrag\n"
    "01.05.2025;KREDITKARTENABRECHNUNG 4711;Sparkasse;-350,00\n"
    "02.05.2025;Miete;Vermieter;-800,00\n"
    "28.05.2025;Gehalt;Arbeitgeber;2.500,00\n"
)
CARD_CSV = (
    "Buchungstag;Verwendungszweck;Betrag\n"
    "20.04.2025;AMAZON EU;-200,00\n"
    "25.04.2025;SHELL TANKSTELLE;-150,00\n"
    "03.05.2025;Zahlung erhalten;350,00\n"
)


def _cats(c):
    return {x["name"]: x["id"] for x in c.get("/api/categories").json()}


def test_credit_card_settlement_is_a_transfer(admin):
    giro = admin.get("/api/accounts").json()[0]
    card = admin.post("/api/accounts", json={"name": "Visa", "kind": "credit_card", "settlement_pattern": " Kreditkarte "}).json()
    assert card["kind"] == "credit_card" and card["settlement_pattern"] == "Kreditkarte"

    _import(admin, giro["id"], GIRO_CSV)
    _import(admin, card["id"], CARD_CSV)
    items = {t["description"]: t for t in admin.get("/api/transactions").json()["items"]}
    assert items["KREDITKARTENABRECHNUNG 4711"]["category_name"] == "Transfer"
    assert items["Zahlung erhalten"]["category_name"] == "Transfer"  # matched by amount and date on the card
    assert items["AMAZON EU"]["category_name"] == "Other"

    # The settlement is not counted again: expenses are rent + the two card purchases.
    s = admin.get("/api/stats/summary").json()
    assert s["expenses"] == 80000 + 20000 + 15000 and s["income"] == 250000

    check = admin.get(f"/api/accounts/{card['id']}/settlements").json()
    assert check == [{"date": "2025-05-01", "amount_cents": -35000, "from_account": giro["name"],
                      "description": "KREDITKARTENABRECHNUNG 4711", "is_transfer": True, "card_payment_date": "2025-05-03"}]
    # Normal accounts have no settlement text.
    assert admin.put(f"/api/accounts/{giro['id']}", json={"name": giro["name"], "settlement_pattern": "x"}).json()["settlement_pattern"] == ""


def test_card_set_up_after_import_applies_to_history(admin):
    giro = admin.get("/api/accounts").json()[0]
    _import(admin, giro["id"], GIRO_CSV)
    card = admin.post("/api/accounts", json={"name": "Visa", "kind": "credit_card", "settlement_pattern": "kreditkarten"}).json()
    assert admin.post(f"/api/accounts/{card['id']}/apply-settlements").json()["updated"] == 1
    items = {t["description"]: t for t in admin.get("/api/transactions").json()["items"]}
    assert items["KREDITKARTENABRECHNUNG 4711"]["category_name"] == "Transfer"
    assert admin.post(f"/api/accounts/{giro['id']}/apply-settlements").status_code == 400


def _amazon(admin) -> dict:
    acc = admin.get("/api/accounts").json()[0]
    return admin.post("/api/transactions", json={
        "account_id": acc["id"], "booking_date": "2025-05-10", "amount_cents": -10000, "description": "AMAZON Order 303-1",
        "category_id": _cats(admin)["Shopping"],
    }).json()


def test_split_validation(admin):
    tx = _amazon(admin)
    cats = _cats(admin)
    put = lambda parts: admin.put(f"/api/transactions/{tx['id']}/splits", json={"parts": parts})  # noqa: E731
    r = put([{"amount_cents": -6000, "category_id": cats["Food & Dining"]}, {"amount_cents": -3000}])
    assert r.status_code == 400 and "-10.00" in r.json()["detail"]  # 10 € left to distribute
    assert put([{"amount_cents": -10000}]).status_code == 400  # one part is not a split
    assert put([{"amount_cents": -12000}, {"amount_cents": 2000}]).status_code == 400  # mixed signs
    assert put([{"amount_cents": -5000, "category_id": 99999}, {"amount_cents": -5000}]).status_code == 404


def test_split_counts_parts_everywhere(admin):
    tx = _amazon(admin)
    cats = _cats(admin)
    r = admin.put(f"/api/transactions/{tx['id']}/splits", json={"parts": [
        {"amount_cents": -4550, "category_id": cats["Shopping"], "note": "USB cable"},
        {"amount_cents": -3000, "category_id": cats["Food & Dining"], "note": "Coffee beans"},
        {"amount_cents": -2450, "category_id": None, "note": "Gift"},
    ]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [(p["note"], p["category_name"]) for p in out["splits"]] == [
        ("USB cable", "Shopping"), ("Coffee beans", "Food & Dining"), ("Gift", None)]

    by_cat = {x["name"]: x["amount"] for x in admin.get("/api/stats/by-category").json()}
    assert by_cat == {"Shopping": 4550, "Food & Dining": 3000, "Uncategorized": 2450}
    assert admin.get("/api/stats/summary").json()["expenses"] == 10000  # total unchanged

    admin.put("/api/budgets", json={"category_id": cats["Food & Dining"], "monthly_limit_cents": 10000})
    assert admin.get("/api/budgets", params={"year": 2025, "month": 5}).json()[0]["spent"] == 3000

    # Filtering by a part's category finds the transaction; the original category no longer matches.
    def ids(cat):
        return [t["id"] for t in admin.get("/api/transactions", params={"category_id": cat}).json()["items"]]
    assert ids(cats["Food & Dining"]) == [tx["id"]]
    assert ids(0) == [tx["id"]]  # the uncategorized part
    assert ids(cats["Investment"]) == []
    assert admin.get("/api/stats/monthly", params={"category_id": cats["Food & Dining"]}).json() == [
        {"month": "2025-05", "income": 0, "expenses": 3000}]

    csv = admin.get("/api/export/csv").text.splitlines()
    assert len(csv) == 4 and "AMAZON Order 303-1 (Coffee beans);;30,00;expense;Food & Dining" in csv[2]

    # Rules do not override split transactions.
    admin.post("/api/rules", json={"field": "description", "pattern": "amazon", "category_id": cats["Travel"]})
    rule = admin.get("/api/rules").json()[0]
    assert admin.post(f"/api/rules/{rule['id']}/apply").json()["updated"] == 0

    # Removing the split brings back the original category.
    r = admin.put(f"/api/transactions/{tx['id']}/splits", json={"parts": []}).json()
    assert r["splits"] == [] and r["category_name"] == "Shopping"
    assert {x["name"] for x in admin.get("/api/stats/by-category").json()} == {"Shopping"}


def test_split_lines_parsing(admin):
    tx = _amazon(admin)
    cats = _cats(admin)
    admin.post("/api/rules", json={"field": "description", "pattern": "kaffee", "category_id": cats["Food & Dining"]})
    text = "Kaffeebohnen 1kg 12,99 €\nUSB-C Kabel; 9.99\n  \nGeschenkpapier x 3\n1.234,50 Laptop\nVersand 5 €\nEUR 3,50 Pfand\n"
    r = admin.post(f"/api/transactions/{tx['id']}/split-lines", json={"text": text}).json()
    assert [(p["note"], p["amount_cents"], p["category_id"]) for p in r["parts"]] == [
        ("Kaffeebohnen 1kg", -1299, cats["Food & Dining"]),
        ("USB-C Kabel", -999, None),
        ("Laptop", -123450, None),
        ("Versand", -500, None),
        ("Pfand", -350, None),
    ]
    assert r["skipped"] == ["Geschenkpapier x 3"]  # a quantity is not a price
    assert admin.post(f"/api/transactions/{tx['id']}/split-lines", json={"text": "no amount here"}).json()["skipped"] == ["no amount here"]


def test_backup_keeps_splits_and_cards(admin):
    import json

    tx = _amazon(admin)
    cats = _cats(admin)
    admin.post("/api/accounts", json={"name": "Visa", "kind": "credit_card", "settlement_pattern": "Kreditkarte"})
    admin.put(f"/api/transactions/{tx['id']}/splits", json={"parts": [
        {"amount_cents": -7000, "category_id": cats["Shopping"], "note": "A"}, {"amount_cents": -3000, "note": "B"}]})
    backup = admin.get("/api/export/json").content
    admin.put(f"/api/transactions/{tx['id']}/splits", json={"parts": []})
    r = admin.post("/api/export/restore", files={"file": ("b.json", backup, "application/json")})
    assert r.status_code == 200, r.text
    t = admin.get("/api/transactions").json()["items"][0]
    assert [(p["amount_cents"], p["category_name"], p["note"]) for p in t["splits"]] == [(-7000, "Shopping", "A"), (-3000, None, "B")]
    visa = next(a for a in admin.get("/api/accounts").json() if a["name"] == "Visa")
    assert visa["kind"] == "credit_card" and visa["settlement_pattern"] == "Kreditkarte"

    broken = json.loads(backup)
    broken["transactions"][0]["splits"][0]["amount_cents"] = -1
    r = admin.post("/api/export/restore", files={"file": ("b.json", json.dumps(broken).encode(), "application/json")})
    assert r.status_code == 400 and "do not add up" in r.json()["detail"]
