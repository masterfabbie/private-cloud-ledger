"""Credit card accounts.

The monthly card settlement on the checking account ("KREDITKARTENABRECHNUNG …") only moves
money to the card; the real purchases are on the card statement. Settlements are therefore
treated as transfers between own accounts, so nothing is counted twice:

- on other accounts: transactions whose description or payer contains the card's settlement text,
- on the card account: incoming payments with the same amount as a settlement (± a few days).
"""

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import models

T = models.Transaction
MATCH_DAYS = 10


def _transfer_category(db: Session, user_id: int) -> models.Category | None:
    return db.scalar(
        select(models.Category).where(models.Category.user_id == user_id, models.Category.kind == "transfer")
        .order_by(models.Category.id)
    )


def card_accounts(db: Session, user_id: int) -> list[models.Account]:
    return list(db.scalars(
        select(models.Account).where(models.Account.user_id == user_id, models.Account.kind == "credit_card")
        .order_by(models.Account.id)
    ))


def settlements(db: Session, card: models.Account, batch_id: int | None = None) -> list[models.Transaction]:
    """Settlement debits for this card on the user's other accounts, newest first."""
    pattern = (card.settlement_pattern or "").strip().lower()
    if not pattern:
        return []
    like = f"%{pattern}%"
    stmt = select(T).where(
        T.user_id == card.user_id, T.account_id != card.id, T.amount_cents < 0,
        or_(func.lower(T.description).like(like), func.lower(T.payer).like(like)),
    )
    if batch_id is not None:
        stmt = stmt.where(T.import_batch_id == batch_id)
    return list(db.scalars(stmt.order_by(T.booking_date.desc(), T.id.desc())).unique())


def _card_payment_for(db: Session, card: models.Account, settlement: models.Transaction) -> models.Transaction | None:
    return db.scalar(
        select(T).where(
            T.account_id == card.id,
            T.amount_cents == -settlement.amount_cents,
            T.booking_date >= settlement.booking_date - timedelta(days=MATCH_DAYS),
            T.booking_date <= settlement.booking_date + timedelta(days=MATCH_DAYS),
        ).order_by(func.abs(func.julianday(T.booking_date) - func.julianday(settlement.booking_date)))
        .limit(1)
    )


def mark_settlements(db: Session, user_id: int, batch_id: int | None = None) -> int:
    """Categorize card settlements (and the matching card payments) as transfers.

    With batch_id only transactions from that import are touched, so later manual
    changes are not overridden on every import. Returns the number of changed transactions.
    """
    transfer = _transfer_category(db, user_id)
    if transfer is None:
        return 0
    changed = 0
    for card in card_accounts(db, user_id):
        for s in settlements(db, card):
            pair = [s, _card_payment_for(db, card, s)]
            for tx in pair:
                if tx is None or tx.splits or tx.category_id == transfer.id:
                    continue
                if batch_id is not None and tx.import_batch_id != batch_id:
                    continue
                tx.category_id = transfer.id
                changed += 1
    db.commit()
    return changed


def settlement_check(db: Session, card: models.Account, limit: int = 12) -> list[dict]:
    out = []
    for s in settlements(db, card)[:limit]:
        payment = _card_payment_for(db, card, s)
        out.append({
            "date": s.booking_date.isoformat(),
            "amount_cents": s.amount_cents,
            "from_account": s.account.name,
            "description": s.description,
            "is_transfer": bool(s.category and s.category.kind == "transfer"),
            "card_payment_date": payment.booking_date.isoformat() if payment else None,
        })
    return out
