"""Bank sync tests against a fake bank that mimics the python-fints client interface."""

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from fints.client import NeedTANResponse
from fints.exceptions import FinTSClientPINError
from fints.models import SEPAAccount

from app import models
from app.services import banksync
from tests.conftest import login

PIN = "12345"
IBAN_GIRO = "DE02120300000000202051"
IBAN_SAVE = "DE88100900001234567892"


@dataclass
class Amount:
    amount: Decimal
    currency: str = "EUR"


class FakeTAN(NeedTANResponse):
    def __init__(self, decoupled: bool, text: str = "Please confirm"):
        self.decoupled = decoupled
        self.challenge = text
        self.challenge_hhduc = None
        self.challenge_matrix = None


class FakeBank:
    """Shared state of the simulated bank for one test."""

    def __init__(self):
        self.mechanisms = {"900": "chipTAN manuell", "946": "pushTAN 2.0"}
        self.login_tan = "decoupled"  # decoupled | text | None
        self.polls_needed = 2  # decoupled: confirmations arrive after this many polls
        self.tx_tan = None  # None | "text": a TAN is needed to read transactions
        self.accounts = [SEPAAccount(IBAN_GIRO, "BYLADEM1001", "202051", None, "12030000"),
                         SEPAAccount(IBAN_SAVE, "BYLADEM1001", "1234567", None, "12030000")]
        today = date.today()
        self.transactions = {
            IBAN_GIRO: [
                {"date": today - timedelta(days=5), "entry_date": today - timedelta(days=5), "amount": Amount(Decimal("-12.99")),
                 "applicant_name": "Netflix International", "applicant_iban": "NL01NFLX0000000001",
                 "purpose": "Abo Netflix", "posting_text": "FOLGELASTSCHRIFT"},
                {"date": today - timedelta(days=3), "guessed_entry_date": today - timedelta(days=3), "amount": Amount(Decimal("2500.00")),
                 "applicant_name": "Arbeitgeber AG", "applicant_iban": "DE44500105175407324931",
                 "purpose": ["Gehalt", "September"], "posting_text": "GUTSCHR. UEBERWEISUNG"},
            ],
            IBAN_SAVE: [],
        }
        self.state_seen = []
        self.tans_received = []
        self.fetch_ranges = []


class FakeClient:
    def __init__(self, bank: FakeBank, conn, pin):
        self.bank, self.pin = bank, pin
        self.selected_tan_medium = conn.tan_medium
        self._mechanism = None
        bank.state_seen.append(conn.client_state)
        if conn.client_state:
            self._mechanism = conn.client_state.decode().split(":")[1]  # restored from deconstruct()
        self.init_tan_response = None
        self._polls = 0

    # TAN setup
    def get_current_tan_mechanism(self):
        return self._mechanism

    def fetch_tan_mechanisms(self):
        return None

    def get_tan_mechanisms(self):
        return {k: type("P", (), {"name": v})() for k, v in self.bank.mechanisms.items()}

    def set_tan_mechanism(self, m):
        self._mechanism = m

    def is_tan_media_required(self):
        return False

    # dialog
    def __enter__(self):
        if self.pin != PIN:
            raise FinTSClientPINError("Error during dialog initialization, PIN wrong?")
        if self.bank.login_tan:
            self.init_tan_response = FakeTAN(self.bank.login_tan == "decoupled", "Login bestätigen")

    def __exit__(self, *exc):
        return False

    def send_tan(self, challenge, tan):
        self.bank.tans_received.append(tan)
        if challenge.decoupled:
            self._polls += 1
            if self._polls < self.bank.polls_needed:
                return FakeTAN(True, challenge.challenge)
            self._polls = 0
            return getattr(challenge, "result", None)
        assert tan == "123456", "wrong TAN"
        return getattr(challenge, "result", None)

    def get_sepa_accounts(self):
        return list(self.bank.accounts)

    def get_transactions(self, account, start, end):
        self.bank.fetch_ranges.append((account.iban, start, end))
        txs = [type("T", (), {"data": d})() for d in self.bank.transactions[account.iban]]
        if self.bank.tx_tan:
            challenge = FakeTAN(False, "TAN für Umsatzabruf")
            challenge.result = txs
            return challenge
        return txs

    def deconstruct(self, including_private=False):
        return f"state:{self._mechanism}".encode()


@pytest.fixture
def engine(tmp_path):
    """A real database file: the sync runs in its own thread with its own connection, as in production."""
    from app.db import Base, make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'bank.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def bank(monkeypatch):
    fake = FakeBank()
    monkeypatch.setattr(banksync, "make_client", lambda conn, pin: FakeClient(fake, conn, pin))
    monkeypatch.setattr(banksync, "DECOUPLED_POLL_SECONDS", 0)
    return fake


def wait_job(c: TestClient, job_id: str, states=("done", "failed", "need_tan", "need_choice", "cancelled"), timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get(f"/api/bank/jobs/{job_id}").json()
        if job["state"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck: {job}")


def connect(c: TestClient, store_pin=True, pin=PIN):
    r = c.post("/api/bank/connections", json={
        "name": "Sparkasse", "blz": "120 300 00", "server_url": "https://banking.example/fints30",
        "login_name": "max", "pin": pin, "store_pin": store_pin,
    })
    assert r.status_code == 201, r.text
    return r.json()


def test_connect_with_pushtan_choice_and_accounts(admin, bank, db):
    giro = admin.post("/api/accounts", json={"name": "Giro", "iban": IBAN_GIRO}).json()
    created = connect(admin)
    job = wait_job(admin, created["job"]["id"])
    assert job["state"] == "need_choice"
    assert {o["label"] for o in job["choice"]["options"]} == {"chipTAN manuell", "pushTAN 2.0"}
    admin.post(f"/api/bank/jobs/{job['id']}/answer", json={"value": "946"})

    job = wait_job(admin, job["id"], states=("done", "failed"))
    assert job["state"] == "done", job
    assert job["result"]["accounts_found"] == 2
    assert bank.tans_received == ["", ""]  # pushTAN: polled twice until confirmed

    conn = admin.get("/api/bank/connections").json()[0]
    assert conn["tan_mechanism"] == "946" and conn["pin_stored"] is True and conn["last_status"] == "ok"
    links = {lk["iban"]: lk for lk in conn["links"]}
    assert links[IBAN_GIRO]["account_id"] == giro["id"]  # matched by IBAN
    assert links[IBAN_SAVE]["account_id"] is None
    assert links[IBAN_GIRO]["sync_from"] == (date.today() - timedelta(days=90)).isoformat()

    # The PIN is stored encrypted, never in plain text.
    stored = db.get(models.BankConnection, conn["id"]).pin_encrypted
    assert stored and PIN not in stored


def test_sync_imports_and_dedups(admin, bank):
    bank.login_tan = None
    giro = admin.post("/api/accounts", json={"name": "Giro", "iban": IBAN_GIRO}).json()
    cats = {c["name"]: c["id"] for c in admin.get("/api/categories").json()}
    admin.post("/api/rules", json={"field": "payer", "pattern": "netflix", "category_id": cats["Entertainment"]})
    created = connect(admin)
    admin.post(f"/api/bank/jobs/{created['job']['id']}/answer", json={"value": "946"}) if wait_job(admin, created["job"]["id"])["state"] == "need_choice" else None
    wait_job(admin, created["job"]["id"], states=("done",))
    conn_id = created["connection"]["id"]

    job = admin.post(f"/api/bank/connections/{conn_id}/sync", json={}).json()
    job = wait_job(admin, job["id"], states=("done", "failed"))
    assert job["state"] == "done", job
    assert job["result"]["imported"] == 2 and job["result"]["duplicates"] == 0

    items = {t["description"]: t for t in admin.get("/api/transactions", params={"account_id": giro["id"]}).json()["items"]}
    netflix = items["Abo Netflix"]
    assert netflix["amount_cents"] == -1299 and netflix["payer"] == "Netflix International"
    assert netflix["category_name"] == "Entertainment"  # rules apply to synced transactions
    assert netflix["tags"] == ["folgelastschrift"]
    assert items["Gehalt September"]["amount_cents"] == 250000

    # A second sync fetches an overlapping window and imports nothing twice.
    job = wait_job(admin, admin.post(f"/api/bank/connections/{conn_id}/sync", json={}).json()["id"], states=("done", "failed"))
    assert job["result"]["imported"] == 0 and job["result"]["duplicates"] == 2
    # The client state from the first session (system ID, TAN method) is reused.
    assert bank.state_seen[-1] == b"state:946"


def test_text_tan_for_transactions_and_link_new_account(admin, bank):
    bank.login_tan = None
    bank.tx_tan = "text"
    created = connect(admin, store_pin=False)
    job = wait_job(admin, created["job"]["id"])
    admin.post(f"/api/bank/jobs/{job['id']}/answer", json={"value": "900"})
    wait_job(admin, job["id"], states=("done",))
    conn = admin.get("/api/bank/connections").json()[0]
    assert conn["pin_stored"] is False
    save_link = next(lk for lk in conn["links"] if lk["iban"] == IBAN_SAVE)
    giro_link = next(lk for lk in conn["links"] if lk["iban"] == IBAN_GIRO)
    r = admin.patch(f"/api/bank/links/{giro_link['id']}", json={"create_account": True, "sync_from": "2025-01-01"})
    assert r.status_code == 200 and r.json()["account_id"] and r.json()["sync_from"] == "2025-01-01"
    assert admin.patch(f"/api/bank/links/{save_link['id']}", json={"account_id": r.json()["account_id"]}).status_code == 409

    # Without a stored PIN the sync needs the PIN.
    assert admin.post(f"/api/bank/connections/{conn['id']}/sync", json={}).status_code == 400
    job = admin.post(f"/api/bank/connections/{conn['id']}/sync", json={"pin": PIN}).json()
    job = wait_job(admin, job["id"])
    assert job["state"] == "need_tan" and job["challenge"]["decoupled"] is False
    assert job["challenge"]["text"] == "TAN für Umsatzabruf"
    admin.post(f"/api/bank/jobs/{job['id']}/answer", json={"value": "123456"})
    job = wait_job(admin, job["id"], states=("done", "failed"))
    assert job["state"] == "done" and job["result"]["imported"] == 2
    assert bank.fetch_ranges[-1][1] == date(2025, 1, 1)


def test_wrong_pin_forgets_stored_pin(admin, bank):
    created = connect(admin, pin="wrong")
    job = wait_job(admin, created["job"]["id"])
    if job["state"] == "need_choice":
        admin.post(f"/api/bank/jobs/{job['id']}/answer", json={"value": "946"})
        job = wait_job(admin, job["id"], states=("done", "failed"))
    assert job["state"] == "failed" and "PIN" in job["message"]
    conn = admin.get("/api/bank/connections").json()[0]
    assert conn["last_status"] == "pin_error" and conn["pin_stored"] is False and conn["auto_sync"] is False


def test_unattended_sync(admin, bank, engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "bank_sync_interval_hours", 6)

    bank.login_tan = None
    admin.post("/api/accounts", json={"name": "Giro", "iban": IBAN_GIRO})
    created = connect(admin)
    job = wait_job(admin, created["job"]["id"])
    admin.post(f"/api/bank/jobs/{job['id']}/answer", json={"value": "946"})
    wait_job(admin, job["id"], states=("done",))
    conn_id = created["connection"]["id"]
    assert admin.patch(f"/api/bank/connections/{conn_id}", json={"auto_sync": True}).json()["auto_sync"] is True

    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        due = banksync.due_connections(s, now=datetime.now(timezone.utc) + timedelta(hours=7))
        assert [c.id for c in due] == [conn_id]

    # Make it due now and run the scheduler body.
    with Session() as s:
        s.get(models.BankConnection, conn_id).last_sync_at = None
        s.commit()
    assert banksync.run_due_syncs(Session) == 1
    conn = admin.get("/api/bank/connections").json()[0]
    assert conn["last_status"] == "ok" and "2 new" in conn["last_message"]

    # If the bank suddenly wants a TAN, unattended sync stops and waits for the user.
    bank.login_tan = "decoupled"
    with Session() as s:
        s.get(models.BankConnection, conn_id).last_sync_at = None
        s.commit()
    banksync.run_due_syncs(Session)
    conn = admin.get("/api/bank/connections").json()[0]
    assert conn["last_status"] == "tan_required"
    with Session() as s:
        assert banksync.due_connections(s) == []  # not retried automatically


def test_isolation_and_cancel(admin, client, bank):
    created = connect(admin)
    job_id = created["job"]["id"]
    wait_job(admin, job_id)  # waiting for the TAN method choice
    admin.post("/api/admin/users", json={"username": "eve", "password": "password123"})
    eve = login(TestClient(client.app), "eve", "password123")
    assert eve.get(f"/api/bank/jobs/{job_id}").status_code == 404
    assert eve.post(f"/api/bank/jobs/{job_id}/answer", json={"value": "946"}).status_code == 409
    assert eve.post(f"/api/bank/connections/{created['connection']['id']}/sync", json={"pin": PIN}).status_code == 404
    assert eve.get("/api/bank/connections").json() == []

    admin.post(f"/api/bank/jobs/{job_id}/cancel")
    assert wait_job(admin, job_id, states=("cancelled",))["state"] == "cancelled"


def test_to_row_mapping_camt_style():
    row = banksync.to_row({
        "amount": Amount(Decimal("-45.30")), "date": date(2025, 5, 2), "entry_date": date(2025, 5, 1),
        "applicant_name": "REWE  Markt", "applicant_iban": "de89 3704 0044 0532 0130 00", "purpose": None,
    }, 1)
    assert row.amount_cents == -4530 and row.booking_date == date(2025, 5, 1)
    assert row.description == "REWE Markt" and row.iban == "DE89370400440532013000" and row.tags == []
