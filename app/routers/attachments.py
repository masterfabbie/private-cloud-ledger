from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.routers.common import owned
from app.services import attachments as att_service

router = APIRouter(prefix="/api", tags=["attachments"])


def attachment_out(a: models.Attachment) -> schemas.AttachmentOut:
    return schemas.AttachmentOut(
        id=a.id, filename=a.filename, content_type=a.content_type, size=a.size,
        url=f"/api/attachments/{a.id}", thumb_url=f"/api/attachments/{a.id}/thumb" if a.has_thumb else None,
        created_at=a.created_at,
    )


@router.post("/transactions/{tx_id}/attachments", response_model=list[schemas.AttachmentOut], status_code=201)
async def upload(
    tx_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    tx = owned(db, models.Transaction, tx_id, user)
    limit = get_settings().max_attachment_mb * 1024 * 1024
    created = []
    try:
        for f in files:
            data = await f.read(limit + 1)
            created.append(att_service.store(db, tx, f.filename or "receipt", data))
        db.commit()
    except att_service.AttachmentError as exc:
        db.rollback()
        att_service.remove_files([(a.user_id, a.stored_name) for a in created])
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return [attachment_out(a) for a in created]


def _send(a: models.Attachment, thumb: bool) -> FileResponse:
    path = att_service.file_path(a, thumb=thumb)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The file is missing on the server")
    return FileResponse(
        path,
        media_type="image/jpeg" if thumb else a.content_type,
        filename=a.filename,
        content_disposition_type="inline",
        headers={
            # Files never change under the same id; never run anything embedded in them.
            "Cache-Control": "private, max-age=86400",
            "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; object-src 'self'",
        },
    )


@router.get("/attachments/{att_id}")
def download(att_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return _send(owned(db, models.Attachment, att_id, user), thumb=False)


@router.get("/attachments/{att_id}/thumb")
def thumbnail(att_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    a = owned(db, models.Attachment, att_id, user)
    if not a.has_thumb:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No preview for this file")
    return _send(a, thumb=True)


@router.delete("/attachments/{att_id}", status_code=204)
def delete(att_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    a = owned(db, models.Attachment, att_id, user)
    files = [(a.user_id, a.stored_name)]
    db.delete(a)
    db.commit()
    att_service.remove_files(files)
