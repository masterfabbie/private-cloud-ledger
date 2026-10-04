import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.db import get_db
from app.routers.common import owned, owned_account
from app.services import recurring, splits as split_service
from app.services.importer import manual_dedup_hash
from app.services.queries import TxFilters, filtered_transactions, tx_filters
from app.services.rules import get_or_create_tags

router = APIRouter(prefix="/api/transactions", tags=["transactions"])


def to_out(tx: models.Transaction) -> schemas.TransactionOut:
    return schemas.TransactionOut(
        id=tx.id,
        account_id=tx.account_id,
        account_name=tx.account.name,
        booking_date=tx.booking_date,
        amount_cents=tx.amount_cents,
        type="income" if tx.amount_cents >= 0 else "expense",
        description=tx.description,
        payer=tx.payer,
        counterparty_iban=tx.counterparty_iban,
        category_id=tx.category_id,
        category_name=tx.category.name if tx.category else None,
        category_color=tx.category.color if tx.category else None,
        notes=tx.notes,
        tags=sorted(t.name for t in tx.tags),
        import_batch_id=tx.import_batch_id,
        splits=[
            schemas.SplitPartOut(
                id=sp.id, amount_cents=sp.amount_cents, category_id=sp.category_id, note=sp.note,
                category_name=sp.category.name if sp.category else None,
                category_color=sp.category.color if sp.category else None,
            )
            for sp in tx.splits
        ],
    )


def _check_category(db: Session, category_id: int | None, user: models.User) -> None:
    if category_id is not None:
        owned(db, models.Category, category_id, user)


@router.get("", response_model=schemas.TransactionPage)
def list_transactions(
    f: TxFilters = Depends(tx_filters),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    stmt = filtered_transactions(user.id, f)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = db.scalars(stmt.offset((page - 1) * page_size).limit(page_size)).unique().all()
    return schemas.TransactionPage(items=[to_out(t) for t in items], total=total, page=page, page_size=page_size)


@router.get("/years", response_model=list[int])
def years(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rows = db.scalars(
        select(func.distinct(func.strftime("%Y", models.Transaction.booking_date))).where(
            models.Transaction.user_id == user.id
        )
    ).all()
    return sorted((int(y) for y in rows if y), reverse=True)


@router.post("", response_model=schemas.TransactionOut, status_code=201)
def create_transaction(
    data: schemas.TransactionIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    owned_account(db, data.account_id, user)
    _check_category(db, data.category_id, user)
    h = manual_dedup_hash(db, data.account_id, data.booking_date, data.amount_cents, data.description, data.payer)
    if h is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This transaction already exists in your records.")
    tx = models.Transaction(
        user_id=user.id,
        **data.model_dump(exclude={"tags"}),
        dedup_hash=h,
    )
    tx.tags = get_or_create_tags(db, user.id, data.tags)
    db.add(tx)
    db.commit()
    db.refresh(tx)
    return to_out(tx)


@router.patch("/{tx_id}", response_model=schemas.TransactionOut)
def update_transaction(
    tx_id: int, data: schemas.TransactionPatch, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    tx = owned(db, models.Transaction, tx_id, user)
    fields = data.model_dump(exclude_unset=True)
    if "account_id" in fields:
        if fields["account_id"] is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "account_id cannot be empty")
        owned_account(db, fields["account_id"], user)
        if fields["account_id"] != tx.account_id:
            tx.dedup_hash = f"moved-{secrets.token_hex(16)}"
    if "category_id" in fields:
        _check_category(db, fields["category_id"], user)
    tags = fields.pop("tags", None)
    for k, v in fields.items():
        if v is None and k != "category_id":
            continue
        setattr(tx, k, v)
    if tags is not None:
        tx.tags = get_or_create_tags(db, user.id, tags)
    db.commit()
    db.refresh(tx)
    return to_out(tx)


@router.put("/{tx_id}/splits", response_model=schemas.TransactionOut)
def set_splits(tx_id: int, data: schemas.SplitsIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Split a transaction into parts with their own categories (an empty list removes the split)."""
    tx = owned(db, models.Transaction, tx_id, user)
    for part in data.parts:
        _check_category(db, part.category_id, user)
    try:
        split_service.set_splits(db, tx, [p.model_dump() for p in data.parts])
    except split_service.SplitError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.commit()
    db.refresh(tx)
    return to_out(tx)


@router.post("/{tx_id}/split-lines")
def split_lines(tx_id: int, data: schemas.SplitTextIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Parse pasted lines ("12,99 Netflix") into split parts with suggested categories."""
    tx = owned(db, models.Transaction, tx_id, user)
    parts, skipped = split_service.parse_lines(data.text, tx, db)
    return {"parts": parts, "skipped": skipped}


@router.post("/bulk-categorize")
def bulk_categorize(
    data: schemas.BulkCategorize, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    _check_category(db, data.category_id, user)
    txs = db.scalars(
        select(models.Transaction).where(
            models.Transaction.user_id == user.id, models.Transaction.id.in_(data.transaction_ids)
        )
    ).all()
    for tx in txs:
        tx.category_id = data.category_id
    db.commit()
    return {"updated": len(txs)}


@router.delete("/{tx_id}", status_code=204)
def delete_transaction(tx_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    tx = owned(db, models.Transaction, tx_id, user)
    db.delete(tx)
    db.commit()


@router.delete("", status_code=200)
def delete_all(
    confirm: str = Query(..., description="Must be 'DELETE'"),
    account_id: int | None = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    if confirm != "DELETE":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pass confirm=DELETE")
    stmt = delete(models.Transaction).where(models.Transaction.user_id == user.id)
    batches = delete(models.ImportBatch).where(models.ImportBatch.user_id == user.id)
    if account_id:
        owned_account(db, account_id, user)
        stmt = stmt.where(models.Transaction.account_id == account_id)
        batches = batches.where(models.ImportBatch.account_id == account_id)
    result = db.execute(stmt)
    db.execute(batches)
    db.commit()
    recurring.detect(db, user.id)
    return {"deleted": result.rowcount}
