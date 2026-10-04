import json

from fastapi.testclient import TestClient

from tests.conftest import login


def _seed(c: TestClient) -> dict:
    acc = c.get("/api/accounts").json()[0]
    savings = c.post("/api/accounts", json={"name": "Savings", "iban": "DE99OWN", "opening_balance_cents": 50000}).json()
    hobby = c.post("/api/categories", json={"name": "Hobby", "color": "#123456"}).json()
    cats = {x["name"]: x["id"] for x in c.get("/api/categories").json()}
    c.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-05-01", "amount_cents": -2000,
                                      "description": "Wool", "payer": "Shop", "category_id": hobby["id"],
                                      "tags": ["knitting", "fun"], "notes": "red"})
    c.post("/api/transactions", json={"account_id": savings["id"], "booking_date": "2025-05-02", "amount_cents": 10000,
                                      "description": "Interest"})
    c.post("/api/rules", json={"pattern": "Shop", "amount_max_cents": 5000, "category_id": hobby["id"], "add_tags": ["fun"]})
    c.put("/api/budgets", json={"category_id": cats["Food & Dining"], "monthly_limit_cents": 30000})
    return {"hobby": hobby["id"]}


def _snapshot(c: TestClient) -> dict:
    """Comparable view of a user's data, without database ids."""
    accounts = {a["id"]: a["name"] for a in c.get("/api/accounts").json()}
    cats = {x["id"]: x["name"] for x in c.get("/api/categories").json()}
    txs = sorted(
        (t["booking_date"], t["amount_cents"], t["description"], t["payer"], t["notes"], accounts[t["account_id"]],
         cats.get(t["category_id"]), tuple(t["tags"]))
        for t in c.get("/api/transactions").json()["items"]
    )
    rules = sorted((r["pattern"], r["amount_max_cents"], cats[r["category_id"]], tuple(r["add_tags"])) for r in c.get("/api/rules").json())
    budgets = sorted((b["category"], b["limit"]) for b in c.get("/api/budgets").json())
    balances = sorted((b["name"], b["balance"]) for b in c.get("/api/stats/balances").json())
    return {"accounts": sorted(accounts.values()), "categories": sorted(cats.values()), "txs": txs,
            "rules": rules, "budgets": budgets, "balances": balances}


def _upload(c: TestClient, content: bytes, dry_run: bool):
    return c.post("/api/export/restore", params={"dry_run": dry_run},
                  files={"file": ("backup.json", content, "application/json")})


def test_roundtrip_restores_everything(admin):
    _seed(admin)
    before = _snapshot(admin)
    backup = admin.get("/api/export/json").content

    # Mess things up after the backup.
    acc = admin.get("/api/accounts").json()[0]
    admin.post("/api/transactions", json={"account_id": acc["id"], "booking_date": "2025-06-01", "amount_cents": -1, "description": "later"})
    admin.post("/api/categories", json={"name": "Temp"})
    for r in admin.get("/api/rules").json():
        admin.delete(f"/api/rules/{r['id']}")
    assert _snapshot(admin) != before

    dry = _upload(admin, backup, dry_run=True).json()
    assert dry["restored"] is False
    assert dry["backup"]["transactions"] == 2 and dry["backup"]["accounts"] == 2 and dry["backup"]["username"] == "admin"
    assert dry["current"]["transactions"] == 3
    assert _snapshot(admin) != before  # a dry run changes nothing

    res = _upload(admin, backup, dry_run=False)
    assert res.status_code == 200 and res.json()["restored"] is True
    assert _snapshot(admin) == before

    # Re-importing a bank file after a restore still detects duplicates (hashes are kept).
    again = admin.get("/api/export/json").json()
    assert sorted(t["dedup_hash"] for t in again["transactions"]) == sorted(t["dedup_hash"] for t in json.loads(backup)["transactions"])


def test_restore_into_another_user(admin, client):
    _seed(admin)
    backup = admin.get("/api/export/json").content
    admin.post("/api/admin/users", json={"username": "newbox", "password": "password123"})
    other = login(TestClient(client.app), "newbox", "password123")
    assert _upload(other, backup, dry_run=False).json()["restored"] is True
    assert _snapshot(other) == _snapshot(admin)
    # The original user's data is untouched and still separate.
    assert len(admin.get("/api/accounts").json()) == 2


def test_bad_files_change_nothing(admin):
    _seed(admin)
    before = _snapshot(admin)
    good = json.loads(admin.get("/api/export/json").content)

    cases = {
        b"not json at all": "not valid JSON",
        json.dumps({"hello": "world"}).encode(): "not a Proud Ledger backup",
        json.dumps({**good, "version": 99}).encode(): "Unsupported backup version",
        json.dumps({**good, "transactions": [{**good["transactions"][0], "account_id": 999}]}).encode(): "account that is not in the backup",
        json.dumps({**good, "transactions": [{**good["transactions"][0], "booking_date": "yesterday"}]}).encode(): "damaged",
        json.dumps({**good, "accounts": []}).encode(): "no accounts",
    }
    for content, message in cases.items():
        r = _upload(admin, content, dry_run=False)
        assert r.status_code == 400, (message, r.text)
        assert message in r.json()["detail"], (message, r.json())
    assert _snapshot(admin) == before


def test_restore_requires_login_and_csrf(client, admin):
    backup = admin.get("/api/export/json").content
    token = admin.headers.pop("X-CSRF-Token")
    assert _upload(admin, backup, dry_run=False).status_code == 403
    admin.headers["X-CSRF-Token"] = token
    anon = TestClient(client.app)
    assert _upload(anon, backup, dry_run=True).status_code == 401
