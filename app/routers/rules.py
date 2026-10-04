from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_current_user
from app.db import get_db
from app.routers.common import owned
from app.services import rules as rule_service

router = APIRouter(prefix="/api/rules", tags=["rules"])


def _validate(db: Session, data: schemas.RuleIn, user: models.User) -> None:
    owned(db, models.Category, data.category_id, user)
    try:
        rule_service.validate_rule(
            data.field, data.match, data.pattern, data.amount_sign, data.amount_min_cents, data.amount_max_cents
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("", response_model=list[schemas.RuleOut])
def list_rules(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return rule_service.load_rules(db, user.id)


@router.post("", response_model=schemas.RuleOut, status_code=201)
def create_rule(data: schemas.RuleIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    _validate(db, data, user)
    rule = models.Rule(user_id=user.id, **data.model_dump())
    db.add(rule)
    db.commit()
    return rule


@router.put("/{rule_id}", response_model=schemas.RuleOut)
def update_rule(
    rule_id: int, data: schemas.RuleIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    rule = owned(db, models.Rule, rule_id, user)
    _validate(db, data, user)
    for k, v in data.model_dump().items():
        setattr(rule, k, v)
    db.commit()
    return rule


@router.delete("/{rule_id}", status_code=204)
def delete_rule(rule_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    db.delete(owned(db, models.Rule, rule_id, user))
    db.commit()


@router.post("/preview")
def preview(data: schemas.RulePreviewIn, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Which existing transactions a rule (as currently edited) would match."""
    try:
        rule_service.validate_rule(
            data.field, data.match, data.pattern, data.amount_sign, data.amount_min_cents, data.amount_max_cents
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    draft = models.Rule(
        user_id=user.id, **data.model_dump(exclude={"rule_id", "category_id", "priority"}), add_tags=[],
        category_id=data.category_id, priority=data.priority,
    )
    matches = rule_service.matching_transactions(db, user.id, draft)
    # Rules that run before this one win at import time; count the matches they would send
    # to a different category.
    earlier = [
        r for r in rule_service.load_rules(db, user.id)
        if r.id != data.rule_id and (r.priority, r.id) < (data.priority, data.rule_id or 10**9)
    ]
    taken = 0
    for t in matches:
        winner = rule_service.first_match(earlier, t.description, t.payer, t.counterparty_iban, t.amount_cents)
        if winner is not None and winner.category_id != data.category_id:
            taken += 1
    return {
        "matches": len(matches),
        "already_in_category": sum(1 for t in matches if data.category_id and t.category_id == data.category_id),
        "taken_by_earlier_rules": taken,
        "sample": [
            {
                "id": t.id,
                "booking_date": t.booking_date.isoformat(),
                "description": t.description,
                "payer": t.payer,
                "amount_cents": t.amount_cents,
                "category_name": t.category.name if t.category else None,
                "category_color": t.category.color if t.category else None,
            }
            for t in matches[:10]
        ],
    }


@router.post("/{rule_id}/apply")
def apply_rule(rule_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Set this rule's category (and tags) on every transaction it matches."""
    rule = owned(db, models.Rule, rule_id, user)
    return {"updated": rule_service.apply_rule_everywhere(db, rule)}


@router.post("/rerun")
def rerun(all_transactions: bool = False, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return {"updated": rule_service.rerun_rules(db, user.id, only_uncategorized=not all_transactions)}


def _similar(db: Session, user: models.User, tx: models.Transaction, field: str) -> list[models.Transaction]:
    T = models.Transaction
    value = {"payer": tx.payer, "iban": tx.counterparty_iban, "description": tx.description}[field]
    if not value.strip():
        return []
    col = {"payer": T.payer, "iban": T.counterparty_iban, "description": T.description}[field]
    return list(db.scalars(select(T).where(T.user_id == user.id, col == value, T.id != tx.id)))


@router.get("/suggest/{tx_id}")
def suggest(tx_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """How many other transactions look like this one (used after an inline category change)."""
    tx = owned(db, models.Transaction, tx_id, user)
    field = "payer" if tx.payer.strip() else ("iban" if tx.counterparty_iban else "description")
    similar = _similar(db, user, tx, field)
    return {
        "field": field,
        "pattern": {"payer": tx.payer, "iban": tx.counterparty_iban, "description": tx.description}[field],
        "similar": len(similar),
        "differently_categorized": sum(1 for t in similar if t.category_id != tx.category_id),
    }


@router.post("/from-transaction", response_model=schemas.RuleOut, status_code=201)
def from_transaction(
    data: schemas.RuleFromTransaction, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    tx = owned(db, models.Transaction, data.transaction_id, user)
    owned(db, models.Category, data.category_id, user)
    if data.field not in rule_service.RULE_FIELDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid field")
    pattern = {"payer": tx.payer, "iban": tx.counterparty_iban, "description": tx.description}[data.field].strip()
    if not pattern:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"The transaction has no {data.field}")
    match = "equals"
    sign = "expense" if tx.amount_cents < 0 else "income"
    rule = db.scalar(
        select(models.Rule).where(
            models.Rule.user_id == user.id,
            models.Rule.field == data.field,
            models.Rule.match == match,
            models.Rule.pattern == pattern,
            models.Rule.amount_sign == sign,
        )
    )
    if rule is None:
        rule = models.Rule(user_id=user.id, field=data.field, match=match, pattern=pattern, amount_sign=sign, category_id=data.category_id, add_tags=[], priority=50)
        db.add(rule)
    else:
        rule.category_id = data.category_id
    tx.category_id = data.category_id
    if data.apply_existing:
        for other in _similar(db, user, tx, data.field):
            if (other.amount_cents < 0) == (tx.amount_cents < 0):
                other.category_id = data.category_id
    db.commit()
    return rule
