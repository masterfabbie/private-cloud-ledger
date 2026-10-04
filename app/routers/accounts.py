from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.db import get_db
from app.routers.common import owned
from app.services import cards

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _clean(data: schemas.AccountIn) -> dict:
    fields = data.model_dump()
    fields["settlement_pattern"] = fields["settlement_pattern"].strip() if data.kind == "credit_card" else ""
    return fields


@router.get("", response_model=list[schemas.AccountOut])
def list_accounts(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return db.scalars(select(models.Account).where(models.Account.user_id == user.id).order_by(models.Account.id)).all()


@router.post("", response_model=schemas.AccountOut, status_code=201)
def create_account(data: schemas.AccountIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    acc = models.Account(user_id=user.id, **_clean(data))
    db.add(acc)
    db.commit()
    return acc


@router.put("/{account_id}", response_model=schemas.AccountOut)
def update_account(
    account_id: int, data: schemas.AccountIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    acc = owned(db, models.Account, account_id, user)
    for k, v in _clean(data).items():
        setattr(acc, k, v)
    db.commit()
    return acc


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    acc = owned(db, models.Account, account_id, user)
    count = db.scalar(select(func.count()).where(models.Account.user_id == user.id))
    if count <= 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You need at least one account")
    db.delete(acc)  # its transactions are deleted via ON DELETE CASCADE
    db.commit()


@router.post("/{account_id}/apply-settlements")
def apply_settlements(account_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Mark all existing settlements of this credit card (and the matching card payments) as transfers."""
    acc = owned(db, models.Account, account_id, user)
    if acc.kind != "credit_card":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only credit card accounts have settlements")
    return {"updated": cards.mark_settlements(db, user.id)}


@router.get("/{account_id}/settlements")
def settlement_check(account_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    acc = owned(db, models.Account, account_id, user)
    return cards.settlement_check(db, acc) if acc.kind == "credit_card" else []
