import re
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import models

RULE_FIELDS = ("description", "payer", "iban")
RULE_MATCHES = ("contains", "equals", "regex")
RULE_SIGNS = ("any", "expense", "income")


def validate_rule(
    field: str,
    match: str,
    pattern: str,
    amount_sign: str,
    amount_min_cents: int | None = None,
    amount_max_cents: int | None = None,
) -> None:
    if field not in RULE_FIELDS:
        raise ValueError(f"field must be one of {RULE_FIELDS}")
    if match not in RULE_MATCHES:
        raise ValueError(f"match must be one of {RULE_MATCHES}")
    if amount_sign not in RULE_SIGNS:
        raise ValueError(f"amount_sign must be one of {RULE_SIGNS}")
    has_amount = amount_min_cents is not None or amount_max_cents is not None
    if not pattern.strip() and not has_amount:
        raise ValueError("Enter a text to match, an amount condition, or both")
    if amount_min_cents is not None and amount_max_cents is not None and amount_min_cents > amount_max_cents:
        raise ValueError("The lower amount must not be larger than the upper amount")
    if match == "regex" and pattern.strip():
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc


def rule_matches(rule: models.Rule, description: str, payer: str, iban: str, amount_cents: int) -> bool:
    if rule.amount_sign == "expense" and amount_cents >= 0:
        return False
    if rule.amount_sign == "income" and amount_cents < 0:
        return False
    if rule.amount_min_cents is not None and abs(amount_cents) < rule.amount_min_cents:
        return False
    if rule.amount_max_cents is not None and abs(amount_cents) > rule.amount_max_cents:
        return False
    if not (rule.pattern or "").strip():
        return True  # amount-only rule
    value = {"description": description, "payer": payer, "iban": iban}.get(rule.field, "") or ""
    pattern = rule.pattern
    if rule.match == "regex":
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            return False
    if rule.field == "iban":
        value, pattern = value.replace(" ", ""), pattern.replace(" ", "")
    if rule.match == "equals":
        return value.strip().lower() == pattern.strip().lower()
    return pattern.strip().lower() in value.lower()


def load_rules(db: Session, user_id: int) -> list[models.Rule]:
    return list(
        db.scalars(select(models.Rule).where(models.Rule.user_id == user_id).order_by(models.Rule.priority, models.Rule.id))
    )


def first_match(
    rules: Iterable[models.Rule], description: str, payer: str, iban: str, amount_cents: int
) -> models.Rule | None:
    return next((r for r in rules if rule_matches(r, description, payer, iban, amount_cents)), None)


def get_or_create_tags(db: Session, user_id: int, names: Iterable[str]) -> list[models.Tag]:
    names = list(dict.fromkeys(n.strip().lower() for n in names if n and n.strip()))
    if not names:
        return []
    existing = {
        t.name: t
        for t in db.scalars(select(models.Tag).where(models.Tag.user_id == user_id, models.Tag.name.in_(names)))
    }
    out = []
    for n in names:
        tag = existing.get(n)
        if tag is None:
            tag = models.Tag(user_id=user_id, name=n[:50])
            db.add(tag)
            db.flush()
            existing[n] = tag
        out.append(tag)
    return out


def apply_rule_to(db: Session, rule: models.Rule, tx: models.Transaction) -> None:
    tx.category_id = rule.category_id
    if rule.add_tags:
        current = {t.name for t in tx.tags}
        for tag in get_or_create_tags(db, tx.user_id, rule.add_tags):
            if tag.name not in current:
                tx.tags.append(tag)


def rerun_rules(db: Session, user_id: int, only_uncategorized: bool = True) -> int:
    """Apply rules to existing transactions. Returns how many were changed."""
    rules = load_rules(db, user_id)
    if not rules:
        return 0
    q = select(models.Transaction).where(models.Transaction.user_id == user_id)
    if only_uncategorized:
        other_ids = select(models.Category.id).where(
            models.Category.user_id == user_id, models.Category.name == "Other"
        )
        q = q.where(or_(models.Transaction.category_id.is_(None), models.Transaction.category_id.in_(other_ids)))
    changed = 0
    for tx in db.scalars(q):
        rule = first_match(rules, tx.description, tx.payer, tx.counterparty_iban, tx.amount_cents)
        if rule and rule.category_id != tx.category_id:
            apply_rule_to(db, rule, tx)
            changed += 1
    db.commit()
    return changed


def matching_transactions(db: Session, user_id: int, rule: models.Rule) -> list[models.Transaction]:
    """All of the user's transactions the rule matches, newest first (rule may be unsaved)."""
    txs = db.scalars(
        select(models.Transaction)
        .where(models.Transaction.user_id == user_id)
        .order_by(models.Transaction.booking_date.desc(), models.Transaction.id.desc())
    ).unique()
    return [t for t in txs if rule_matches(rule, t.description, t.payer, t.counterparty_iban, t.amount_cents)]


def apply_rule_everywhere(db: Session, rule: models.Rule) -> int:
    """Apply a rule to every matching transaction, whatever its current category."""
    changed = 0
    for tx in matching_transactions(db, rule.user_id, rule):
        before = (tx.category_id, {t.name for t in tx.tags})
        apply_rule_to(db, rule, tx)
        if (tx.category_id, {t.name for t in tx.tags}) != before:
            changed += 1
    db.commit()
    return changed
