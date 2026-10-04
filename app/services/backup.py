"""Per-user JSON backup: export and restore.

A restore replaces all of the user's data (accounts, categories, transactions, tags, rules,
budgets, recurring series) in one database transaction. The file is fully validated before
anything is deleted, so a broken or foreign file leaves the existing data untouched.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import models

BACKUP_VERSION = 1


class BackupError(ValueError):
    """The file cannot be restored; the message is shown to the user."""


# ---------------------------------------------------------------- export


def export_data(db: Session, user: models.User) -> dict:
    def dump(model, **extra):
        cols = [c.name for c in model.__table__.columns if c.name not in ("user_id", "password_hash")]
        out = []
        for obj in db.scalars(select(model).where(model.user_id == user.id).order_by(model.id)):
            row = {c: getattr(obj, c) for c in cols}
            row.update({k: fn(obj) for k, fn in extra.items()})
            out.append(row)
        return out

    return {
        "version": BACKUP_VERSION,
        "exported_at": date.today().isoformat(),
        "username": user.username,
        "accounts": dump(models.Account),
        "categories": dump(models.Category),
        "transactions": dump(
            models.Transaction,
            tags=lambda t: sorted(tag.name for tag in t.tags),
            splits=lambda t: [{"amount_cents": sp.amount_cents, "category_id": sp.category_id, "note": sp.note} for sp in t.splits],
        ),
        "rules": dump(models.Rule),
        "budgets": dump(models.Budget),
        "recurring": dump(models.RecurringSeries),
    }


# ---------------------------------------------------------------- file format


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BkAccount(_Row):
    id: int
    name: str
    bank: str = ""
    iban: str = ""
    currency: str = "EUR"
    opening_balance_cents: int = 0
    opening_date: date | None = None
    kind: Literal["checking", "savings", "credit_card", "cash"] = "checking"
    settlement_pattern: str = ""


class BkCategory(_Row):
    id: int
    name: str
    color: str = "#667eea"
    kind: Literal["expense", "income", "transfer"] = "expense"


class BkSplit(_Row):
    amount_cents: int
    category_id: int | None = None
    note: str = ""


class BkTransaction(_Row):
    id: int
    account_id: int
    booking_date: date
    amount_cents: int
    description: str = ""
    payer: str = ""
    counterparty_iban: str = ""
    category_id: int | None = None
    notes: str = ""
    dedup_hash: str
    created_at: datetime | None = None
    tags: list[str] = []
    splits: list[BkSplit] = []


class BkRule(_Row):
    field: str = "payer"
    match: str = "contains"
    pattern: str = ""
    amount_sign: str = "any"
    amount_min_cents: int | None = None
    amount_max_cents: int | None = None
    category_id: int
    add_tags: list[str] = []
    priority: int = 100


class BkBudget(_Row):
    category_id: int
    monthly_limit_cents: int


class BkRecurring(_Row):
    payer_key: str
    display_name: str
    typical_amount_cents: int
    interval_days: int
    occurrences: int = 0
    last_date: date
    next_date: date
    category_id: int | None = None
    status: Literal["detected", "confirmed", "dismissed"] = "detected"


class Backup(_Row):
    version: int
    exported_at: str | None = None
    username: str | None = None
    accounts: list[BkAccount] = []
    categories: list[BkCategory] = []
    transactions: list[BkTransaction] = []
    rules: list[BkRule] = []
    budgets: list[BkBudget] = []
    recurring: list[BkRecurring] = []


def parse_backup(data: object) -> Backup:
    if not isinstance(data, dict) or "version" not in data or "transactions" not in data:
        raise BackupError("This is not a Proud Ledger backup file.")
    if data.get("version") != BACKUP_VERSION:
        raise BackupError(f"Unsupported backup version {data.get('version')!r}; this app reads version {BACKUP_VERSION}.")
    try:
        bk = Backup.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise BackupError(f"The backup file is damaged ({where}: {first['msg']}).") from exc
    _check_references(bk)
    return bk


def _check_references(bk: Backup) -> None:
    account_ids = {a.id for a in bk.accounts}
    category_ids = {c.id for c in bk.categories}
    if not bk.accounts:
        raise BackupError("The backup contains no accounts.")
    names = [c.name.strip().lower() for c in bk.categories]
    if len(names) != len(set(names)):
        raise BackupError("The backup contains two categories with the same name.")
    seen_hashes: set[tuple[int, str]] = set()
    for t in bk.transactions:
        if t.account_id not in account_ids:
            raise BackupError(f"Transaction {t.id} refers to an account that is not in the backup.")
        if t.category_id is not None and t.category_id not in category_ids:
            raise BackupError(f"Transaction {t.id} refers to a category that is not in the backup.")
        for sp in t.splits:
            if sp.category_id is not None and sp.category_id not in category_ids:
                raise BackupError(f"A split of transaction {t.id} refers to a category that is not in the backup.")
        if t.splits and sum(sp.amount_cents for sp in t.splits) != t.amount_cents:
            raise BackupError(f"The split parts of transaction {t.id} do not add up to its amount.")
        key = (t.account_id, t.dedup_hash)
        if key in seen_hashes:
            raise BackupError(f"Transaction {t.id} is duplicated in the backup.")
        seen_hashes.add(key)
    for r in bk.rules:
        if r.category_id not in category_ids:
            raise BackupError("A rule refers to a category that is not in the backup.")
    budget_cats = [b.category_id for b in bk.budgets]
    if any(c not in category_ids for c in budget_cats) or len(budget_cats) != len(set(budget_cats)):
        raise BackupError("The budgets in the backup refer to unknown or duplicate categories.")
    for s in bk.recurring:
        if s.category_id is not None and s.category_id not in category_ids:
            raise BackupError("A subscription refers to a category that is not in the backup.")


# ---------------------------------------------------------------- restore


def summarize(bk: Backup) -> dict:
    return {
        "exported_at": bk.exported_at,
        "username": bk.username,
        "accounts": len(bk.accounts),
        "categories": len(bk.categories),
        "transactions": len(bk.transactions),
        "rules": len(bk.rules),
        "budgets": len(bk.budgets),
        "recurring": len(bk.recurring),
    }


def current_counts(db: Session, user_id: int) -> dict:
    def count(model) -> int:
        return db.scalar(select(func.count()).select_from(model).where(model.user_id == user_id)) or 0

    return {
        "accounts": count(models.Account),
        "categories": count(models.Category),
        "transactions": count(models.Transaction),
        "rules": count(models.Rule),
        "budgets": count(models.Budget),
        "recurring": count(models.RecurringSeries),
    }


def restore(db: Session, user: models.User, bk: Backup) -> None:
    """Replace all of the user's data with the backup. Commits on success, rolls back on error."""
    uid = user.id
    try:
        for model in (
            models.Transaction, models.ImportBatch, models.RecurringSeries, models.Budget,
            models.Rule, models.Category, models.Account, models.Tag,
        ):
            db.execute(delete(model).where(model.user_id == uid))
        db.flush()

        account_map: dict[int, int] = {}
        for a in bk.accounts:
            acc = models.Account(user_id=uid, **a.model_dump(exclude={"id"}))
            db.add(acc)
            db.flush()
            account_map[a.id] = acc.id

        category_map: dict[int, int] = {}
        for c in bk.categories:
            cat = models.Category(user_id=uid, **c.model_dump(exclude={"id"}))
            db.add(cat)
            db.flush()
            category_map[c.id] = cat.id

        tags: dict[str, models.Tag] = {}

        def tag(name: str) -> models.Tag:
            key = name.strip().lower()[:50]
            if key not in tags:
                tags[key] = models.Tag(user_id=uid, name=key)
                db.add(tags[key])
            return tags[key]

        for t in bk.transactions:
            fields = t.model_dump(exclude={"id", "tags", "account_id", "category_id", "created_at", "splits"})
            tx = models.Transaction(
                user_id=uid,
                account_id=account_map[t.account_id],
                category_id=category_map.get(t.category_id) if t.category_id is not None else None,
                **fields,
            )
            if t.created_at is not None:
                tx.created_at = t.created_at
            tx.tags = [tag(n) for n in dict.fromkeys(t.tags) if n.strip()]
            tx.splits = [
                models.TransactionSplit(
                    user_id=uid, amount_cents=sp.amount_cents, note=sp.note,
                    category_id=category_map.get(sp.category_id) if sp.category_id is not None else None,
                )
                for sp in t.splits
            ]
            db.add(tx)

        for r in bk.rules:
            db.add(models.Rule(user_id=uid, **{**r.model_dump(), "category_id": category_map[r.category_id]}))
        for b in bk.budgets:
            db.add(models.Budget(user_id=uid, category_id=category_map[b.category_id], monthly_limit_cents=b.monthly_limit_cents))
        for s in bk.recurring:
            data = s.model_dump()
            data["category_id"] = category_map.get(s.category_id) if s.category_id is not None else None
            db.add(models.RecurringSeries(user_id=uid, **data))
        db.commit()
    except Exception:
        db.rollback()
        raise
