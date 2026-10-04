from fastapi.testclient import TestClient

from tests.conftest import login


def _make_user(admin: TestClient, name: str) -> None:
    r = admin.post("/api/admin/users", json={"username": name, "password": "password123"})
    assert r.status_code == 201, r.text


def _new_client(client: TestClient) -> TestClient:
    return TestClient(client.app)


def test_login_required(client):
    assert client.get("/api/transactions").status_code == 401
    assert client.get("/api/health").json() == {"status": "ok"}


def test_bad_login_and_throttle(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin-password"}).status_code == 429


def test_csrf_required(admin):
    token = admin.headers.pop("X-CSRF-Token")
    assert admin.post("/api/categories", json={"name": "X"}).status_code == 403
    admin.headers["X-CSRF-Token"] = token
    assert admin.post("/api/categories", json={"name": "X"}).status_code == 201


def test_user_isolation(admin, client):
    _make_user(admin, "alice")
    _make_user(admin, "bob")
    alice = login(_new_client(client), "alice", "password123")
    bob = login(_new_client(client), "bob", "password123")

    acc = alice.get("/api/accounts").json()[0]
    r = alice.post("/api/transactions", json={
        "account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": -1234, "description": "Secret",
    })
    assert r.status_code == 201
    tx_id = r.json()["id"]

    assert bob.get("/api/transactions").json()["total"] == 0
    assert bob.patch(f"/api/transactions/{tx_id}", json={"description": "hacked"}).status_code == 404
    assert bob.delete(f"/api/transactions/{tx_id}").status_code == 404
    bob_acc = bob.get("/api/accounts").json()[0]
    assert bob_acc["id"] != acc["id"]
    # Bob cannot add a transaction into Alice's account.
    assert bob.post("/api/transactions", json={
        "account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": 1, "description": "x",
    }).status_code == 404
    assert bob.get("/api/stats/summary").json()["expenses"] == 0
    assert alice.get("/api/stats/summary").json()["expenses"] == 1234
    # Non-admins cannot manage users.
    assert bob.get("/api/admin/users").status_code == 403


def test_manual_duplicate_rejected(admin):
    acc = admin.get("/api/accounts").json()[0]
    body = {"account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": -500, "description": "Pizza"}
    assert admin.post("/api/transactions", json=body).status_code == 201
    assert admin.post("/api/transactions", json=body).status_code == 409


def test_import_flow_and_undo(admin):
    acc = admin.get("/api/accounts").json()[0]
    csv = "Buchungstag;Verwendungszweck;Betrag\n01.05.2025;Miete;-800,00\n02.05.2025;Gehalt;2.500,00\n".encode("cp1252")
    r = admin.post("/api/imports/preview", files={"file": ("bank.csv", csv, "application/vnd.ms-excel")}, data={"account_id": acc["id"]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["mapping"]["date"] == "Buchungstag"
    assert p["total_rows"] == 2

    body = {"token": p["token"], "account_id": acc["id"], "mapping": p["mapping"]}
    dry = admin.post("/api/imports/commit?dry_run=true", json=body).json()
    assert dry["valid"] == 2
    res = admin.post("/api/imports/commit", json=body).json()
    assert res["imported"] == 2

    summary = admin.get("/api/stats/summary", params={"year": 2025, "month": 5}).json()
    assert summary == {"income": 250000, "expenses": 80000, "balance": 170000, "transfers": 0}
    # The month filter also works without a year (it was ignored in the old version).
    assert admin.get("/api/stats/summary", params={"month": 5}).json()["income"] == 250000
    assert admin.get("/api/stats/summary", params={"month": 6}).json()["income"] == 0

    # Second import of the same file: profile is found, everything is a duplicate.
    p2 = admin.post("/api/imports/preview", files={"file": ("bank.csv", csv, "text/csv")}, data={"account_id": acc["id"]}).json()
    assert p2["profile_found"] is True
    res2 = admin.post("/api/imports/commit", json={"token": p2["token"], "account_id": acc["id"], "mapping": p2["mapping"]}).json()
    assert (res2["imported"], res2["duplicates"]) == (0, 2)

    batches = admin.get("/api/imports").json()
    assert len(batches) == 1
    assert admin.delete(f"/api/imports/{batches[0]['id']}").json()["deleted"] == 2
    assert admin.get("/api/transactions").json()["total"] == 0


def test_exports(admin):
    acc = admin.get("/api/accounts").json()[0]
    admin.post("/api/transactions", json={
        "account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": -1250, "description": "=cmd()", "tags": ["a"],
    })
    csv = admin.get("/api/export/csv", params={"year": 2025, "month": 5})
    assert csv.status_code == 200
    assert "transactions_2025_May_" in csv.headers["content-disposition"]
    text = csv.text
    assert "12,50" in text and "'=cmd()" in text
    assert admin.get("/api/export/xlsx").status_code == 200
    backup = admin.get("/api/export/json").json()
    assert backup["transactions"][0]["tags"] == ["a"]


def test_rule_from_transaction(admin):
    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    ids = []
    for d in ("2025-05-01", "2025-05-08"):
        r = admin.post("/api/transactions", json={
            "account_id": acc["id"], "booking_date": d, "amount_cents": -2000, "description": "Einkauf", "payer": "REWE",
        })
        ids.append(r.json()["id"])
    s = admin.get(f"/api/rules/suggest/{ids[0]}").json()
    assert s["field"] == "payer" and s["similar"] == 1
    r = admin.post("/api/rules/from-transaction", json={"transaction_id": ids[0], "category_id": cats["Food & Dining"]})
    assert r.status_code == 201
    items = admin.get("/api/transactions").json()["items"]
    assert {t["category_name"] for t in items} == {"Food & Dining"}


def test_budgets(admin):
    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    admin.post("/api/transactions", json={
        "account_id": acc["id"], "booking_date": "2025-05-03", "amount_cents": -9000, "description": "Dinner",
        "category_id": cats["Food & Dining"],
    })
    assert admin.put("/api/budgets", json={"category_id": cats["Food & Dining"], "monthly_limit_cents": 10000}).status_code == 200
    b = admin.get("/api/budgets", params={"year": 2025, "month": 5}).json()
    assert b[0]["spent"] == 9000 and b[0]["percent"] == 90.0


def test_admin_cannot_demote_self_and_deactivate_logs_out(admin, client):
    me = admin.get("/api/auth/me").json()
    assert admin.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False}).status_code == 400
    _make_user(admin, "carol")
    carol = login(_new_client(client), "carol", "password123")
    carol_id = next(u["id"] for u in admin.get("/api/admin/users").json() if u["username"] == "carol")
    admin.patch(f"/api/admin/users/{carol_id}", json={"is_active": False})
    assert carol.get("/api/auth/me").status_code == 401


def test_income_in_expense_category_does_not_hide_expenses(admin):
    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    for amount, desc in ((-85000, "Rent"), (310000, "Salary")):
        admin.post("/api/transactions", json={
            "account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": amount, "description": desc,
            "category_id": cats["Other"],
        })
    by_cat = admin.get("/api/stats/by-category").json()
    assert by_cat == [{"category_id": cats["Other"], "name": "Other", "color": "#C9CBCF", "amount": 85000}]
    admin.put("/api/budgets", json={"category_id": cats["Other"], "monthly_limit_cents": 100000})
    assert admin.get("/api/budgets", params={"year": 2025, "month": 5}).json()[0]["spent"] == 85000


def test_rule_amount_conditions(admin):
    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}

    # Amount-only rule: rent is exactly 850,00 € (expense).
    r = admin.post("/api/rules", json={"pattern": "", "amount_min_cents": 85000, "amount_max_cents": 85000,
                                       "amount_sign": "expense", "category_id": cats["Monthly Bills"]})
    assert r.status_code == 201, r.text
    # Text + amount range: small REWE purchases are food, big ones shopping.
    admin.post("/api/rules", json={"field": "payer", "pattern": "REWE", "amount_max_cents": 5000,
                                   "category_id": cats["Food & Dining"], "priority": 10})
    admin.post("/api/rules", json={"field": "payer", "pattern": "REWE", "amount_min_cents": 5001,
                                   "category_id": cats["Shopping"], "priority": 20})
    rules = admin.get("/api/rules").json()
    assert {(x["amount_min_cents"], x["amount_max_cents"]) for x in rules} == {(85000, 85000), (None, 5000), (5001, None)}

    for amount, payer, desc in ((-85000, "Vermieter", "Miete"), (-85000, "Vermieter", "Miete Juni"), (85000, "X", "Erstattung"),
                                (-2350, "REWE", "Einkauf"), (-12000, "REWE", "Großeinkauf"), (-5000, "REWE", "Grenze")):
        admin.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-05-01",
                                              "amount_cents": amount, "payer": payer, "description": desc})
    assert admin.post("/api/rules/rerun").json()["updated"] == 5
    got = {t["description"]: t["category_name"] for t in admin.get("/api/transactions").json()["items"]}
    assert got["Miete"] == got["Miete Juni"] == "Monthly Bills"
    assert got["Erstattung"] is None  # income, rule is for expenses only
    assert got["Einkauf"] == "Food & Dining"
    assert got["Grenze"] == "Food & Dining"  # bounds are inclusive
    assert got["Großeinkauf"] == "Shopping"


def test_rule_validation(admin):
    cat = admin.get("/api/categories").json()[0]["id"]
    assert admin.post("/api/rules", json={"pattern": "", "category_id": cat}).status_code == 400
    r = admin.post("/api/rules", json={"pattern": "x", "amount_min_cents": 500, "amount_max_cents": 100, "category_id": cat})
    assert r.status_code == 400 and "lower amount" in r.json()["detail"]
    assert admin.post("/api/rules", json={"pattern": "x", "amount_min_cents": -1, "category_id": cat}).status_code == 422


def test_rule_preview_and_apply(admin):
    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    for d, amount, payer in (("2025-05-01", -2000, "REWE"), ("2025-05-08", -9000, "REWE"),
                             ("2025-05-09", -1500, "Lidl"), ("2025-05-10", 500, "REWE")):
        admin.post("/api/transactions", json={"account_id": acc["id"], "booking_date": d, "amount_cents": amount,
                                              "payer": payer, "description": "Einkauf",
                                              "category_id": cats["Shopping"] if amount == -9000 else None})
    draft = {"field": "payer", "pattern": "rewe", "amount_sign": "expense", "category_id": cats["Food & Dining"]}
    p = admin.post("/api/rules/preview", json=draft).json()
    assert p["matches"] == 2 and p["already_in_category"] == 0 and p["taken_by_earlier_rules"] == 0
    assert [t["booking_date"] for t in p["sample"]] == ["2025-05-08", "2025-05-01"]  # newest first
    assert p["sample"][0]["category_name"] == "Shopping"

    # Nothing is saved by a preview.
    assert admin.get("/api/rules").json() == []
    # Invalid input is reported, not raised.
    bad = admin.post("/api/rules/preview", json={"pattern": "(", "match": "regex"})
    assert bad.status_code == 400 and "regular expression" in bad.json()["detail"]

    # An earlier (lower priority number) rule that also matches is reported.
    admin.post("/api/rules", json={"field": "payer", "pattern": "REWE", "amount_min_cents": 5000,
                                   "category_id": cats["Shopping"], "priority": 10})
    p = admin.post("/api/rules/preview", json={**draft, "priority": 100}).json()
    assert p["taken_by_earlier_rules"] == 1
    # An earlier rule that leads to the same category is not a conflict.
    same = admin.post("/api/rules/preview", json={**draft, "category_id": cats["Shopping"]}).json()
    assert same["taken_by_earlier_rules"] == 0

    # Save and apply to every match, including the one already in another category.
    rule = admin.post("/api/rules", json={**draft, "add_tags": ["groceries"]}).json()
    assert admin.post(f"/api/rules/{rule['id']}/apply").json()["updated"] == 2
    items = {t["booking_date"]: t for t in admin.get("/api/transactions").json()["items"]}
    assert items["2025-05-08"]["category_name"] == items["2025-05-01"]["category_name"] == "Food & Dining"
    assert items["2025-05-01"]["tags"] == ["groceries"]
    assert items["2025-05-10"]["category_name"] is None  # income is excluded by the rule
    assert admin.post(f"/api/rules/{rule['id']}/apply").json()["updated"] == 0  # idempotent


def test_delete_all_rules_and_subscriptions(admin, client, db):
    from datetime import date

    from app import models

    acc = admin.get("/api/accounts").json()[0]
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    for p in ("REWE", "Lidl", "Aldi"):
        admin.post("/api/rules", json={"pattern": p, "category_id": cats["Food & Dining"]})
    tx = admin.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": -500,
                                               "description": "Einkauf", "payer": "REWE", "category_id": cats["Food & Dining"]}).json()
    me = admin.get("/api/auth/me").json()
    for i, status in enumerate(("detected", "confirmed", "dismissed")):
        db.add(models.RecurringSeries(user_id=me["id"], payer_key=f"-s{i}", display_name=f"S{i}", typical_amount_cents=-999,
                                      interval_days=30, last_date=date(2025, 5, 1), next_date=date(2025, 6, 1), status=status))
    db.commit()

    # Another user's data must survive.
    admin.post("/api/admin/users", json={"username": "other", "password": "password123"})
    other = login(TestClient(client.app), "other", "password123")
    other_cat = other.get("/api/categories").json()[0]["id"]
    other.post("/api/rules", json={"pattern": "x", "category_id": other_cat})

    assert admin.delete("/api/rules", params={"confirm": "nope"}).status_code == 400
    assert admin.delete("/api/rules", params={"confirm": "DELETE"}).json() == {"deleted": 3}
    assert admin.get("/api/rules").json() == []
    assert len(other.get("/api/rules").json()) == 1
    # Transactions keep their categories.
    assert admin.get("/api/transactions").json()["items"][0]["category_name"] == "Food & Dining"

    assert admin.delete("/api/recurring", params={"confirm": "DELETE"}).json() == {"deleted": 3}
    assert admin.get("/api/recurring").json() == []
    assert admin.get("/api/transactions").json()["total"] == 1 and tx["id"]
