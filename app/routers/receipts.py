"""Scan a receipt photo/PDF, match it to a transaction, attach it and suggest split parts."""

import secrets
import time
from dataclasses import dataclass
from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.routers.attachments import attachment_out
from app.routers.common import owned, owned_account
from app.routers.transactions import to_out
from app.services import attachments as att_service
from app.services import receipts
from app.services.importer import manual_dedup_hash

router = APIRouter(prefix="/api", tags=["receipts"])

TTL = 3600


@dataclass
class _Pending:
    user_id: int
    prepared: att_service.Prepared
    created: float


# Scanned files wait here until the user picks the transaction (single process, like CSV uploads).
_pending: dict[str, _Pending] = {}


def _cleanup() -> None:
    cutoff = time.time() - TTL
    for k in [k for k, p in _pending.items() if p.created < cutoff]:
        _pending.pop(k, None)


class NewTransaction(BaseModel):
    account_id: int
    booking_date: date
    amount_cents: int
    description: str = Field(min_length=1, max_length=2000)
    payer: str = ""


class AttachIn(BaseModel):
    transaction_id: int | None = None
    create: NewTransaction | None = None


@router.get("/receipts/config")
def config(_=Depends(get_current_user)):
    p = receipts.provider()
    return {"enabled": p != "off", "provider": p, "label": receipts.PROVIDERS.get(p, ""),
            "leaves_server": p == "claude"}


async def _read(prepared: att_service.Prepared) -> receipts.ReceiptData:
    try:
        # Model calls and OCR take seconds; keep the event loop free meanwhile.
        return await run_in_threadpool(receipts.scan, prepared.content, prepared.content_type)
    except receipts.ReceiptScanError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.post("/receipts/scan")
async def scan(file: UploadFile = File(...), db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _cleanup()
    limit = get_settings().max_attachment_mb * 1024 * 1024
    try:
        prepared = att_service.prepare(file.filename or "receipt", await file.read(limit + 1))
    except att_service.AttachmentError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    data = await _read(prepared)
    token = secrets.token_urlsafe(16)
    _pending[token] = _Pending(user.id, prepared, time.time())
    return {
        "token": token,
        "receipt": receipts.to_dict(data),
        "parts": receipts.suggested_parts(db, user.id, data),
        "candidates": [to_out(t) for t in receipts.candidates(db, user.id, data)],
    }


@router.post("/receipts/{token}/attach")
def attach(token: str, data: AttachIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    pending = _pending.get(token)
    if pending is None or pending.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The scanned receipt expired, please scan it again")
    if data.transaction_id:
        tx = owned(db, models.Transaction, data.transaction_id, user)
    elif data.create:
        c = data.create
        owned_account(db, c.account_id, user)
        h = manual_dedup_hash(db, c.account_id, c.booking_date, c.amount_cents, c.description, c.payer)
        if h is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This transaction already exists; choose it from the list instead.")
        tx = models.Transaction(user_id=user.id, account_id=c.account_id, booking_date=c.booking_date,
                                amount_cents=c.amount_cents, description=c.description, payer=c.payer, dedup_hash=h)
        db.add(tx)
        db.flush()
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a transaction or create a new one")
    try:
        att = att_service.save(tx, pending.prepared)
        db.commit()
    except att_service.AttachmentError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    _pending.pop(token, None)
    db.refresh(tx)
    return {"transaction": to_out(tx), "attachment": attachment_out(att)}


@router.post("/attachments/{att_id}/scan")
async def scan_attachment(att_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Read a receipt that is already attached, e.g. to fill the split editor with its items."""
    a = owned(db, models.Attachment, att_id, user)
    path = att_service.file_path(a)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file is missing on the server")
    prepared = att_service.Prepared(a.filename, a.content_type, path.read_bytes(), None)
    data = await _read(prepared)
    return {"receipt": receipts.to_dict(data), "parts": receipts.suggested_parts(db, user.id, data)}
