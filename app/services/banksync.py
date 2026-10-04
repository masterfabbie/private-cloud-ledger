"""Bank sync via FinTS 3.0 (HBCI PIN/TAN) using python-fints.

A sync runs as a *job* in a background thread, because the bank dialog has to stay open
while the user confirms a TAN (e.g. in the S-pushTAN app). The web UI polls the job and
answers its questions (which TAN method, which TAN). The app runs as one process, so jobs
live in memory.

Fetched transactions go through the regular importer: duplicates, rules, transfer and
subscription detection all apply.
"""

from __future__ import annotations

import base64
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import FinTSClientPINError, FinTSClientTemporaryAuthError
from fints.models import SEPAAccount
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.services import importer, recurring, secretbox
from app.version import app_version

log = logging.getLogger(__name__)

ANSWER_TIMEOUT = 300  # seconds to wait for a TAN or a choice
DECOUPLED_POLL_SECONDS = 3
DEFAULT_HISTORY_DAYS = 90  # most banks allow this much without an extra TAN


class NeedsUser(Exception):
    """Raised in unattended mode when the bank asks for a TAN or a choice."""


class JobCancelled(Exception):
    pass


# ---------------------------------------------------------------- jobs


@dataclass
class Job:
    id: str
    user_id: int
    connection_id: int
    kind: str  # connect | sync
    interactive: bool = True
    state: str = "running"  # running | need_tan | need_choice | done | failed | cancelled
    message: str = "Connecting to the bank…"
    challenge: dict | None = None
    choice: dict | None = None
    result: dict | None = None
    created: float = field(default_factory=time.time)
    _answer: str | None = None
    _event: threading.Event = field(default_factory=threading.Event)
    _cancelled: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "kind": self.kind,
            "state": self.state,
            "message": self.message,
            "challenge": self.challenge,
            "choice": self.choice,
            "result": self.result,
        }

    # -- called from the worker thread

    def progress(self, message: str) -> None:
        self.state, self.message, self.challenge, self.choice = "running", message, None, None

    def wait_answer(self) -> str:
        self._event.clear()
        self._answer = None
        deadline = time.time() + ANSWER_TIMEOUT
        while not self._event.wait(0.5):
            if self._cancelled:
                raise JobCancelled()
            if time.time() > deadline:
                raise TimeoutError("No answer within 5 minutes; the bank session was closed.")
        if self._cancelled:
            raise JobCancelled()
        return self._answer or ""

    def ask_choice(self, title: str, options: list[dict]) -> str:
        if not self.interactive:
            raise NeedsUser("The bank needs a choice; open Proud Ledger and click “Sync now”.")
        self.state, self.choice, self.challenge = "need_choice", {"title": title, "options": options}, None
        self.message = title
        answer = self.wait_answer()
        if answer not in {o["id"] for o in options}:
            raise ValueError("Invalid choice")
        self.progress("Continuing…")
        return answer

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise JobCancelled()

    # -- called from the web request

    def answer(self, value: str) -> None:
        self._answer = value
        self._event.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def get_job(job_id: str, user_id: int) -> Job | None:
    job = _jobs.get(job_id)
    return job if job and job.user_id == user_id else None


def active_job_for(connection_id: int) -> Job | None:
    with _jobs_lock:
        for job in _jobs.values():
            if job.connection_id == connection_id and job.state in ("running", "need_tan", "need_choice"):
                return job
    return None


def _cleanup_jobs() -> None:
    cutoff = time.time() - 3600
    with _jobs_lock:
        for k in [k for k, j in _jobs.items() if j.created < cutoff and j.state in ("done", "failed", "cancelled")]:
            del _jobs[k]


def start_job(session_factory, connection: models.BankConnection, kind: str, pin: str | None, *, interactive=True) -> Job:
    _cleanup_jobs()
    existing = active_job_for(connection.id)
    if existing:
        return existing
    job = Job(id=secrets.token_urlsafe(12), user_id=connection.user_id, connection_id=connection.id, kind=kind,
              interactive=interactive)
    with _jobs_lock:
        _jobs[job.id] = job
    thread = threading.Thread(target=run_job, args=(session_factory, job, pin), daemon=True, name=f"banksync-{job.id}")
    thread.start()
    return job


# ---------------------------------------------------------------- FinTS session


def make_client(conn: models.BankConnection, pin: str):
    """Build the python-fints client (patched in tests)."""
    settings = get_settings()
    return FinTS3PinTanClient(
        conn.blz,
        conn.login_name,
        pin,
        conn.server_url,
        customer_id=conn.customer_id or None,
        tan_medium=conn.tan_medium,
        product_id=settings.fints_product_id,
        product_version=app_version()[:5],
        from_data=conn.client_state,
    )


def _challenge_info(resp: NeedTANResponse) -> dict:
    info = {
        "text": resp.challenge or "",
        "decoupled": bool(resp.decoupled),
        "image": None,
        "flicker": bool(resp.challenge_hhduc),
    }
    if resp.challenge_matrix:
        mime, data = resp.challenge_matrix
        info["image"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return info


def resolve_tan(client, resp, job: Job):
    """Answer TAN requests until the bank returns the actual result."""
    started = time.time()
    while isinstance(resp, NeedTANResponse):
        if not job.interactive:
            raise NeedsUser("The bank asks for a TAN; open Proud Ledger and click “Sync now”.")
        job.state, job.challenge, job.choice = "need_tan", _challenge_info(resp), None
        if resp.decoupled:
            job.message = "Please confirm in your banking app (e.g. S-pushTAN)."
            # Poll the bank until the user confirmed in the app.
            if time.time() - started > ANSWER_TIMEOUT:
                raise TimeoutError("The confirmation in the app did not arrive within 5 minutes.")
            for _ in range(DECOUPLED_POLL_SECONDS * 2):
                time.sleep(0.5)
                job.check_cancelled()
            resp = client.send_tan(resp, "")
        else:
            job.message = "Please enter the TAN."
            tan = job.wait_answer().strip()
            job.progress("Checking the TAN…")
            resp = client.send_tan(resp, tan)
    if job.state == "need_tan":
        job.progress("Confirmed. Continuing…")
    return resp


def setup_tan_method(client, conn: models.BankConnection, job: Job) -> None:
    """Pick the TAN method (and TAN medium) on first use, like python-fints' bootstrap helper."""
    if not client.get_current_tan_mechanism():
        client.fetch_tan_mechanisms()
        mechanisms = list(client.get_tan_mechanisms().items())
        if conn.tan_mechanism and conn.tan_mechanism in dict(mechanisms):
            client.set_tan_mechanism(conn.tan_mechanism)
        elif len(mechanisms) > 1:
            choice = job.ask_choice(
                "Which TAN method do you want to use?",
                [{"id": key, "label": str(getattr(p, "name", key))} for key, p in mechanisms],
            )
            client.set_tan_mechanism(choice)
        elif len(mechanisms) == 1:
            client.set_tan_mechanism(mechanisms[0][0])
    conn.tan_mechanism = client.get_current_tan_mechanism()

    try:
        media_required = client.selected_tan_medium is None and client.is_tan_media_required()
    except KeyError:  # current mechanism unknown to the bank parameters (e.g. single-step)
        media_required = False
    if media_required:
        _, media = client.get_tan_media()
        if len(media) == 0:
            client.selected_tan_medium = ""  # e.g. some Sparkassen with pushTAN
        elif len(media) == 1:
            client.set_tan_medium(media[0])
        else:
            names = [m.tan_medium_name for m in media]
            choice = job.ask_choice(
                "Which device should receive the TAN?",
                [{"id": n, "label": n} for n in names],
            )
            client.set_tan_medium(media[names.index(choice)])
    conn.tan_medium = client.selected_tan_medium


def _txt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value if v)
    return " ".join(str(value).split())


def to_row(data: dict, line: int) -> importer.Row:
    """Map a python-fints transaction (MT940 or CAMT) to an importer row."""
    amount = data.get("amount")
    value = getattr(amount, "amount", amount)
    cents = int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    booked = data.get("entry_date") or data.get("guessed_entry_date") or data.get("date")
    if isinstance(booked, datetime):
        booked = booked.date()
    booked = date(booked.year, booked.month, booked.day)
    payer = _txt(data.get("applicant_name"))
    purpose = _txt(data.get("purpose"))
    posting = _txt(data.get("posting_text"))
    return importer.Row(
        line=line,
        booking_date=booked,
        amount_cents=cents,
        description=purpose or posting or payer or "Bank transaction",
        payer=payer,
        iban=_txt(data.get("applicant_iban")).replace(" ", "").upper()[:34],
        tags=[posting.lower()[:50]] if posting else [],
    )


def _sepa(link: models.BankAccountLink) -> SEPAAccount:
    return SEPAAccount(link.iban or None, link.bic or None, link.account_number, link.subaccount or None, link.blz)


def default_sync_from(db: Session, account_id: int | None) -> date:
    """Start after the newest transaction already in the account, so CSV history and bank sync don't overlap."""
    if account_id:
        newest = db.scalar(select(func.max(models.Transaction.booking_date)).where(models.Transaction.account_id == account_id))
        if newest:
            return newest + timedelta(days=1)
    return date.today() - timedelta(days=DEFAULT_HISTORY_DAYS)


# ---------------------------------------------------------------- job bodies


def _connect(db: Session, client, conn: models.BankConnection, job: Job) -> dict:
    job.progress("Loading your accounts…")
    accounts = resolve_tan(client, client.get_sepa_accounts(), job)
    existing = {(lk.iban, lk.account_number, lk.subaccount): lk for lk in conn.links}
    own = {a.iban.replace(" ", "").upper(): a.id for a in db.scalars(select(models.Account).where(models.Account.user_id == conn.user_id)) if a.iban}
    found = 0
    for acc in accounts:
        key = (acc.iban or "", acc.accountnumber or "", acc.subaccount or "")
        if key in existing:
            continue
        account_id = own.get((acc.iban or "").replace(" ", "").upper())
        conn.links.append(models.BankAccountLink(
            user_id=conn.user_id, iban=acc.iban or "", bic=acc.bic or "", account_number=acc.accountnumber or "",
            subaccount=acc.subaccount or "", blz=acc.blz or conn.blz, account_id=account_id,
            sync_from=default_sync_from(db, account_id),
        ))
        found += 1
    return {"accounts_found": len(accounts), "new_accounts": found}


def _sync(db: Session, client, conn: models.BankConnection, job: Job) -> dict:
    links = [lk for lk in conn.links if lk.account_id]
    if not links:
        return {"accounts": [], "imported": 0, "duplicates": 0,
                "note": "No bank account is linked to a Proud Ledger account yet."}
    user = db.get(models.User, conn.user_id)
    today = date.today()
    summary = {"accounts": [], "imported": 0, "duplicates": 0}
    for lk in links:
        account = db.get(models.Account, lk.account_id)
        start = lk.sync_from or today - timedelta(days=DEFAULT_HISTORY_DAYS)
        if lk.synced_until:
            # Re-read a few days: banks sometimes book late.
            start = max(start, lk.synced_until - timedelta(days=14))
        job.progress(f"Fetching transactions for {account.name} since {start:%d.%m.%Y}…")
        txs = resolve_tan(client, client.get_transactions(_sepa(lk), start, today), job)
        rows, failures = [], []
        for i, t in enumerate(txs or []):
            data = getattr(t, "data", t)
            try:
                row = to_row(data, i + 1)
            except Exception as exc:  # malformed entry from the bank
                failures.append({"line": i + 1, "error": str(exc)[:200]})
                continue
            if row.booking_date >= start:
                rows.append(row)
        res = importer.import_rows(db, user, account, rows, failures, f"Bank sync: {conn.name}")
        lk.synced_until = today
        summary["accounts"].append({"account": account.name, "fetched": len(rows), "imported": res.imported,
                                    "duplicates": res.duplicates, "failed": res.failed})
        summary["imported"] += res.imported
        summary["duplicates"] += res.duplicates
    if summary["imported"]:
        recurring.detect(db, conn.user_id)
    return summary


def run_job(session_factory, job: Job, pin: str | None) -> None:
    db: Session = session_factory()
    conn = None
    try:
        conn = db.get(models.BankConnection, job.connection_id)
        if conn is None:
            raise RuntimeError("Bank connection not found")
        if not get_settings().fints_product_id:
            raise RuntimeError("FINTS_PRODUCT_ID is not set. Add your FinTS product registration number to .env.")
        if pin is None:
            if not conn.pin_encrypted:
                raise NeedsUser("The PIN is needed; click “Sync now” and enter it.")
            pin = secretbox.decrypt(conn.pin_encrypted)

        client = make_client(conn, pin)
        setup_tan_method(client, conn, job)
        with client:
            if getattr(client, "init_tan_response", None) is not None:
                job.progress("The bank asks for a confirmation to log in…")
                resolve_tan(client, client.init_tan_response, job)
            result = _connect(db, client, conn, job)
            if job.kind == "sync":
                result.update(_sync(db, client, conn, job))
        conn.client_state = client.deconstruct(including_private=True)
        conn.last_status, conn.last_message = "ok", _summary_text(job.kind, result)
        conn.last_sync_at = datetime.now(timezone.utc)
        db.commit()
        job.result, job.state, job.message = result, "done", conn.last_message
    except JobCancelled:
        db.rollback()
        job.state, job.message = "cancelled", "Cancelled."
    except NeedsUser as exc:
        db.rollback()
        _mark(db, job.connection_id, "tan_required", str(exc))
        job.state, job.message = "failed", str(exc)
    except FinTSClientPINError:
        db.rollback()
        msg = ("The bank rejected the PIN. A stored PIN was removed and automatic sync turned off, "
               "so your online banking does not get locked. Check the PIN and try again.")
        _mark(db, job.connection_id, "pin_error", msg, forget_pin=True)
        job.state, job.message = "failed", msg
    except FinTSClientTemporaryAuthError:
        db.rollback()
        msg = "The bank reports your online banking access as temporarily locked. Please check with your bank."
        _mark(db, job.connection_id, "pin_error", msg, forget_pin=True)
        job.state, job.message = "failed", msg
    except secretbox.SecretUnavailable as exc:
        db.rollback()
        _mark(db, job.connection_id, "error", str(exc))
        job.state, job.message = "failed", str(exc)
    except Exception as exc:
        log.exception("Bank sync failed")
        db.rollback()
        msg = f"The bank sync failed: {str(exc)[:300] or exc.__class__.__name__}"
        _mark(db, job.connection_id, "error", msg)
        job.state, job.message = "failed", msg
    finally:
        job.challenge = job.challenge if job.state == "need_tan" else None
        db.close()


def _summary_text(kind: str, r: dict) -> str:
    if kind == "connect":
        return f"Connected. Found {r['accounts_found']} account(s) at the bank."
    if r.get("note"):
        return r["note"]
    return f"Synced: {r['imported']} new transaction(s), {r['duplicates']} already known."


def _mark(db: Session, connection_id: int, status: str, message: str, forget_pin: bool = False) -> None:
    conn = db.get(models.BankConnection, connection_id)
    if conn is None:
        return
    conn.last_status, conn.last_message = status, message
    if forget_pin:
        conn.pin_encrypted = None
        conn.auto_sync = False
    db.commit()


# ---------------------------------------------------------------- unattended sync


def due_connections(db: Session, now: datetime | None = None) -> list[models.BankConnection]:
    hours = get_settings().bank_sync_interval_hours
    if hours <= 0:
        return []
    now = now or datetime.now(timezone.utc)
    out = []
    for conn in db.scalars(select(models.BankConnection).where(
        models.BankConnection.auto_sync.is_(True), models.BankConnection.pin_encrypted.is_not(None),
    )):
        if conn.last_status in ("pin_error", "tan_required"):
            continue  # needs the user first; never retry a rejected PIN automatically
        last = conn.last_sync_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last is None or now - last >= timedelta(hours=hours):
            out.append(conn)
    return out


def run_due_syncs(session_factory) -> int:
    """Run all due automatic syncs one after another in the calling thread."""
    db = session_factory()
    try:
        due = [(c.id, c.user_id) for c in due_connections(db)]
    finally:
        db.close()
    for conn_id, user_id in due:
        if active_job_for(conn_id):
            continue
        job = Job(id=secrets.token_urlsafe(12), user_id=user_id, connection_id=conn_id, kind="sync", interactive=False)
        with _jobs_lock:
            _jobs[job.id] = job
        run_job(session_factory, job, None)
    return len(due)


def start_scheduler(session_factory) -> threading.Event | None:
    if get_settings().bank_sync_interval_hours <= 0:
        return None
    stop = threading.Event()

    def loop():
        stop.wait(60)  # let the app start first
        while not stop.is_set():
            try:
                run_due_syncs(session_factory)
            except Exception:
                log.exception("Automatic bank sync failed")
            stop.wait(15 * 60)

    threading.Thread(target=loop, daemon=True, name="banksync-scheduler").start()
    return stop
