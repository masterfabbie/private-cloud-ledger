import secrets
import time
from dataclasses import dataclass

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.routers.common import owned, owned_account
from app.services import csv_parser as cp
from app.services import attachments as att_service
from app.services import importer, recurring

router = APIRouter(prefix="/api/imports", tags=["import"])

UPLOAD_TTL = 3600


@dataclass
class _Upload:
    user_id: int
    filename: str
    raw: bytes
    created: float


# Uploaded files wait here between preview and commit. The app runs as a single process,
# so an in-memory store is enough and nothing sensitive is written to disk.
_uploads: dict[str, _Upload] = {}


def _cleanup() -> None:
    cutoff = time.time() - UPLOAD_TTL
    for k in [k for k, u in _uploads.items() if u.created < cutoff]:
        _uploads.pop(k, None)


def _get_upload(token: str, user: models.User) -> _Upload:
    up = _uploads.get(token)
    if up is None or up.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Upload expired, please choose the file again")
    return up


def _parse(up: _Upload, encoding: str | None, delimiter: str | None) -> cp.ParsedCSV:
    try:
        return cp.parse_csv(up.raw, encoding=encoding or None, delimiter=delimiter or None)
    except (cp.ParseError, LookupError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/preview")
async def preview(
    file: UploadFile | None = File(None),
    token: str | None = Form(None),
    account_id: int | None = Form(None),
    encoding: str | None = Form(None),
    delimiter: str | None = Form(None),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _cleanup()
    if file is not None:
        limit = get_settings().max_upload_mb * 1024 * 1024
        raw = await file.read(limit + 1)
        if len(raw) > limit:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File is too large")
        token = secrets.token_urlsafe(16)
        _uploads[token] = _Upload(user.id, file.filename or "upload.csv", raw, time.time())
    elif not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Send a file")
    up = _get_upload(token, user)

    profile = None
    if account_id and not (encoding or delimiter):
        owned_account(db, account_id, user)
        first = _parse(up, None, None)
        profile = db.scalar(
            select(models.ImportProfile).where(
                models.ImportProfile.account_id == account_id, models.ImportProfile.header_signature == first.signature
            )
        )
        if profile:
            encoding = profile.mapping.get("encoding")
            delimiter = profile.mapping.get("delimiter")
    parsed = _parse(up, encoding, delimiter)
    mapping = dict(profile.mapping) if profile else cp.suggest_mapping(parsed.headers)
    return {
        "token": token,
        "filename": up.filename,
        "encoding": parsed.encoding,
        "delimiter": parsed.delimiter,
        "header_line": parsed.header_line,
        "headers": parsed.headers,
        "rows": parsed.rows[:5],
        "total_rows": len(parsed.rows),
        "mapping": mapping,
        "profile_found": profile is not None,
    }


@router.post("/commit")
def commit(
    data: schemas.ImportCommit,
    dry_run: bool = False,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    up = _get_upload(data.token, user)
    account = owned_account(db, data.account_id, user)
    parsed = _parse(up, data.mapping.get("encoding"), data.mapping.get("delimiter"))
    try:
        rows, failures = importer.normalize_rows(parsed, data.mapping)
    except cp.ParseError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if dry_run:
        return {
            "rows": [
                {
                    "line": r.line,
                    "date": r.booking_date.isoformat(),
                    "amount_cents": r.amount_cents,
                    "description": r.description,
                    "payer": r.payer,
                    "category": r.category,
                    "tags": r.tags,
                }
                for r in rows[:5]
            ],
            "valid": len(rows),
            "failed": len(failures),
            "failed_rows": failures[:20],
        }

    if data.save_profile:
        profile_mapping = {**data.mapping, "encoding": parsed.encoding, "delimiter": parsed.delimiter}
        profile = db.scalar(
            select(models.ImportProfile).where(
                models.ImportProfile.account_id == account.id,
                models.ImportProfile.header_signature == parsed.signature,
            )
        )
        if profile:
            profile.mapping = profile_mapping
        else:
            db.add(models.ImportProfile(account_id=account.id, header_signature=parsed.signature, mapping=profile_mapping))

    result = importer.import_rows(db, user, account, rows, failures, up.filename)
    if result.imported:
        recurring.detect(db, user.id)
    _uploads.pop(data.token, None)
    return {
        "imported": result.imported,
        "duplicates": result.duplicates,
        "failed": result.failed,
        "failed_rows": result.failed_rows[:50],
        "batch_id": result.batch_id,
    }


@router.get("")
def list_batches(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.execute(
        select(models.ImportBatch, models.Account.name)
        .join(models.Account, models.ImportBatch.account_id == models.Account.id)
        .where(models.ImportBatch.user_id == user.id)
        .order_by(models.ImportBatch.created_at.desc())
        .limit(100)
    ).all()
    return [
        {
            "id": b.id,
            "account": name,
            "filename": b.filename,
            "imported": b.imported,
            "duplicates": b.duplicates,
            "failed": b.failed,
            "created_at": b.created_at.isoformat(),
        }
        for b, name in rows
    ]


@router.delete("/{batch_id}")
def undo_batch(batch_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    batch = owned(db, models.ImportBatch, batch_id, user)
    files = att_service.files_of(db, models.Transaction.import_batch_id == batch.id, models.Transaction.user_id == user.id)
    res = db.execute(
        delete(models.Transaction).where(
            models.Transaction.import_batch_id == batch.id, models.Transaction.user_id == user.id
        )
    )
    db.delete(batch)
    db.commit()
    att_service.remove_files(files)
    recurring.detect(db, user.id)
    return {"deleted": res.rowcount}
