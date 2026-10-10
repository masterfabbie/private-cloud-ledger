from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import hash_password, require_admin, validate_password
from app.db import get_db
from app.services import attachments as att_service
from app.services.defaults import seed_user_defaults

router = APIRouter(prefix="/api/admin/users", tags=["admin"])


@router.get("", response_model=list[schemas.UserOut])
def list_users(db: Session = Depends(get_db), _=Depends(require_admin)):
    return [schemas.UserOut.of(u) for u in db.scalars(select(models.User).order_by(models.User.id))]


@router.post("", response_model=schemas.UserOut, status_code=201)
def create_user(data: schemas.UserCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    validate_password(data.password)
    if db.scalar(select(models.User).where(func.lower(models.User.username) == data.username.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    user = models.User(username=data.username, password_hash=hash_password(data.password), is_admin=data.is_admin)
    db.add(user)
    db.flush()
    seed_user_defaults(db, user)
    db.commit()
    return schemas.UserOut.of(user)


def _get(db: Session, user_id: int) -> models.User:
    user = db.get(models.User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


@router.patch("/{user_id}", response_model=schemas.UserOut)
def update_user(
    user_id: int, data: schemas.UserUpdate, db: Session = Depends(get_db), admin: models.User = Depends(require_admin)
):
    user = _get(db, user_id)
    if user.id == admin.id and (data.is_admin is False or data.is_active is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot remove your own admin rights or deactivate yourself")
    if data.is_admin is not None:
        user.is_admin = data.is_admin
    if data.is_active is not None:
        user.is_active = data.is_active
        if not data.is_active:
            db.execute(delete(models.Session).where(models.Session.user_id == user.id))
    if data.password:
        validate_password(data.password)
        user.password_hash = hash_password(data.password)
        db.execute(delete(models.Session).where(models.Session.user_id == user.id))
    db.commit()
    return schemas.UserOut.of(user)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    user = _get(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete yourself")
    files = att_service.files_of(db, models.Transaction.user_id == user.id)
    db.delete(user)  # all of the user's data is removed via ON DELETE CASCADE
    db.commit()
    att_service.remove_files(files)
