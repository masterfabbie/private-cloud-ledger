from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import db as db_module
from app import models
from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.routers.common import owned, owned_account
from app.services import banksync, secretbox

router = APIRouter(prefix="/api/bank", tags=["bank sync"])


def _session_factory():
    # Looked up at call time so tests can swap the session factory.
    return db_module.SessionLocal()


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    blz: str = Field(pattern=r"^\d{8}$")
    server_url: str = Field(pattern=r"^https://", max_length=255)
    login_name: str = Field(min_length=1, max_length=100)
    customer_id: str = ""
    pin: str = Field(min_length=1, max_length=64)
    store_pin: bool = False

    @field_validator("blz", mode="before")
    @classmethod
    def _blz(cls, v):
        return str(v).replace(" ", "")


class ConnectionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    server_url: str | None = Field(None, pattern=r"^https://", max_length=255)
    auto_sync: bool | None = None
    forget_pin: bool = False


class SyncIn(BaseModel):
    pin: str | None = Field(None, max_length=64)
    store_pin: bool = False


class LinkPatch(BaseModel):
    account_id: int | None = None
    create_account: bool = False
    sync_from: date | None = None


class AnswerIn(BaseModel):
    value: str = Field(max_length=200)


def _link_out(lk: models.BankAccountLink) -> dict:
    return {
        "id": lk.id,
        "iban": lk.iban,
        "account_number": lk.account_number,
        "account_id": lk.account_id,
        "sync_from": lk.sync_from.isoformat() if lk.sync_from else None,
        "synced_until": lk.synced_until.isoformat() if lk.synced_until else None,
    }


def _conn_out(c: models.BankConnection) -> dict:
    job = banksync.active_job_for(c.id)
    return {
        "id": c.id,
        "name": c.name,
        "blz": c.blz,
        "server_url": c.server_url,
        "login_name": c.login_name,
        "pin_stored": c.pin_encrypted is not None,
        "auto_sync": c.auto_sync,
        "tan_mechanism": c.tan_mechanism,
        "last_sync_at": c.last_sync_at.isoformat() if c.last_sync_at else None,
        "last_status": c.last_status,
        "last_message": c.last_message,
        "active_job": job.id if job else None,
        "links": [_link_out(lk) for lk in c.links],
    }


def _store_pin(conn: models.BankConnection, pin: str) -> None:
    try:
        conn.pin_encrypted = secretbox.encrypt(pin)
    except secretbox.SecretUnavailable as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/config")
def config(_=Depends(get_current_user)):
    s = get_settings()
    return {
        "product_id_configured": bool(s.fints_product_id),
        "can_store_pin": secretbox.can_store_secrets(),
        "sync_interval_hours": s.bank_sync_interval_hours,
    }


@router.get("/connections")
def list_connections(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    conns = db.scalars(select(models.BankConnection).where(models.BankConnection.user_id == user.id).order_by(models.BankConnection.id))
    return [_conn_out(c) for c in conns]


@router.post("/connections", status_code=201)
def create_connection(data: ConnectionIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if not get_settings().fints_product_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bank sync needs FINTS_PRODUCT_ID in .env.")
    conn = models.BankConnection(
        user_id=user.id, name=data.name, blz=data.blz, server_url=data.server_url.strip(),
        login_name=data.login_name.strip(), customer_id=data.customer_id.strip(),
    )
    if data.store_pin:
        _store_pin(conn, data.pin)
    db.add(conn)
    db.commit()
    job = banksync.start_job(_session_factory, conn, "connect", data.pin)
    return {"connection": _conn_out(conn), "job": job.public()}


@router.patch("/connections/{conn_id}")
def update_connection(conn_id: int, data: ConnectionPatch, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    conn = owned(db, models.BankConnection, conn_id, user)
    if data.name is not None:
        conn.name = data.name
    if data.server_url is not None:
        conn.server_url = data.server_url.strip()
    if data.forget_pin:
        conn.pin_encrypted = None
        conn.auto_sync = False
    if data.auto_sync is not None:
        if data.auto_sync and not conn.pin_encrypted:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Automatic sync needs a stored PIN.")
        conn.auto_sync = data.auto_sync
        if data.auto_sync and conn.last_status == "tan_required":
            conn.last_status = "ok"
    db.commit()
    return _conn_out(conn)


@router.delete("/connections/{conn_id}", status_code=204)
def delete_connection(conn_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    conn = owned(db, models.BankConnection, conn_id, user)
    job = banksync.active_job_for(conn.id)
    if job:
        job.cancel()
    db.delete(conn)  # imported transactions stay
    db.commit()


@router.post("/connections/{conn_id}/sync")
def sync(conn_id: int, data: SyncIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    conn = owned(db, models.BankConnection, conn_id, user)
    if not data.pin and not conn.pin_encrypted:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Please enter your online-banking PIN.")
    if data.pin and data.store_pin:
        _store_pin(conn, data.pin)
        db.commit()
    job = banksync.start_job(_session_factory, conn, "sync", data.pin or None)
    return job.public()


@router.patch("/links/{link_id}")
def update_link(link_id: int, data: LinkPatch, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    lk = owned(db, models.BankAccountLink, link_id, user)
    fields = data.model_dump(exclude_unset=True)
    if data.create_account:
        acc = models.Account(user_id=user.id, name=f"{lk.connection.name} {lk.iban[-4:] or lk.account_number}", bank=lk.connection.name, iban=lk.iban)
        db.add(acc)
        db.flush()
        lk.account_id = acc.id
        lk.sync_from = data.sync_from or banksync.default_sync_from(db, None)
    elif "account_id" in fields:
        if data.account_id is not None:
            owned_account(db, data.account_id, user)
            taken = db.scalar(select(models.BankAccountLink.id).where(
                models.BankAccountLink.account_id == data.account_id, models.BankAccountLink.id != lk.id))
            if taken:
                raise HTTPException(status.HTTP_409_CONFLICT, "That account is already linked to another bank account.")
        if data.account_id != lk.account_id:
            lk.account_id = data.account_id
            lk.synced_until = None
            if "sync_from" not in fields:
                lk.sync_from = banksync.default_sync_from(db, data.account_id)
    if "sync_from" in fields and data.sync_from:
        lk.sync_from = data.sync_from
        lk.synced_until = None
    db.commit()
    return _link_out(lk)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: models.User = Depends(get_current_user)):
    job = banksync.get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sync job not found (it may have expired)")
    return job.public()


@router.post("/jobs/{job_id}/answer")
def answer(job_id: str, data: AnswerIn, user: models.User = Depends(get_current_user)):
    job = banksync.get_job(job_id, user.id)
    if job is None or job.state not in ("need_tan", "need_choice"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This sync is not waiting for an answer")
    job.answer(data.value)
    return job.public()


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, user: models.User = Depends(get_current_user)):
    job = banksync.get_job(job_id, user.id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sync job not found")
    job.cancel()
    return job.public()
