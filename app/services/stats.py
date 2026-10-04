from calendar import monthrange
from collections import defaultdict
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import models
from app.services.queries import TxFilters, apply_filters

T = models.Transaction
C = models.Category
S = models.TransactionSplit


def _rows(db: Session, user_id: int, f: TxFilters, skip_categories: bool = False):
    """(date, amount, category_id, category kind) per *effective line*: split transactions
    contribute one line per part, all others one line. Category filters apply to the lines."""
    category = case((S.id.is_not(None), S.category_id), else_=T.category_id)
    stmt = (
        select(T.booking_date, func.coalesce(S.amount_cents, T.amount_cents), category, C.kind)
        .select_from(T)
        .outerjoin(S, S.transaction_id == T.id)
        .outerjoin(C, C.id == category)
    )
    rows = db.execute(apply_filters(stmt, user_id, f, skip_categories=True)).all()
    if f.category_ids and not skip_categories:
        wanted = set(f.category_ids)
        rows = [r for r in rows if (r[2] or 0) in wanted]
    return rows


def summary(db: Session, user_id: int, f: TxFilters) -> dict:
    income = expenses = transfers = 0
    for _, cents, _, kind in _rows(db, user_id, f):
        if kind == "transfer":
            transfers += cents
        elif cents >= 0:
            income += cents
        else:
            expenses += -cents
    return {"income": income, "expenses": expenses, "balance": income - expenses, "transfers": transfers}


def monthly(db: Session, user_id: int, f: TxFilters) -> list[dict]:
    """Income/expenses per month. Honours category filters (the line-chart filter buttons)."""
    data: dict[str, dict] = defaultdict(lambda: {"income": 0, "expenses": 0})
    for d, cents, _, kind in _rows(db, user_id, f):
        if kind == "transfer":
            continue
        key = f"{d.year}-{d.month:02d}"
        if cents >= 0:
            data[key]["income"] += cents
        else:
            data[key]["expenses"] += -cents
    return [{"month": k, **v} for k, v in sorted(data.items())]


def by_category(db: Session, user_id: int, f: TxFilters) -> list[dict]:
    """Expenses per category (only outgoing amounts, like the original tracker)."""
    totals: dict[int | None, int] = defaultdict(int)
    for _, cents, cat_id, kind in _rows(db, user_id, f):
        if kind == "transfer" or cents >= 0:
            continue
        totals[cat_id] -= cents
    cats = {c.id: c for c in db.scalars(select(C).where(C.user_id == user_id))}
    out = []
    for cat_id, cents in totals.items():
        if cents <= 0:
            continue
        cat = cats.get(cat_id)
        out.append(
            {
                "category_id": cat_id,
                "name": cat.name if cat else "Uncategorized",
                "color": cat.color if cat else "#C9CBCF",
                "amount": cents,
            }
        )
    return sorted(out, key=lambda r: -r["amount"])


def account_balances(db: Session, user_id: int) -> list[dict]:
    accounts = db.scalars(select(models.Account).where(models.Account.user_id == user_id).order_by(models.Account.id))
    sums = dict(
        db.execute(
            select(T.account_id, func.coalesce(func.sum(T.amount_cents), 0))
            .where(T.user_id == user_id)
            .group_by(T.account_id)
        ).all()
    )
    return [
        {"account_id": a.id, "name": a.name, "balance": a.opening_balance_cents + int(sums.get(a.id, 0))}
        for a in accounts
    ]


def balance_history(db: Session, user_id: int, account_id: int | None = None) -> list[dict]:
    """End-of-day balance for each day that has transactions."""
    accounts = select(models.Account).where(models.Account.user_id == user_id)
    if account_id:
        accounts = accounts.where(models.Account.id == account_id)
    opening = sum(a.opening_balance_cents for a in db.scalars(accounts))
    stmt = select(T.booking_date, func.sum(T.amount_cents)).where(T.user_id == user_id)
    if account_id:
        stmt = stmt.where(T.account_id == account_id)
    stmt = stmt.group_by(T.booking_date).order_by(T.booking_date)
    running = opening
    out = []
    for d, cents in db.execute(stmt).all():
        running += int(cents)
        out.append({"date": d.isoformat(), "balance": running})
    return out


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def budget_status(db: Session, user_id: int, year: int, month: int) -> list[dict]:
    start, end = month_bounds(year, month)
    spent: dict[int | None, int] = defaultdict(int)
    for _, cents, cat_id, _ in _rows(db, user_id, TxFilters(date_from=start, date_to=end)):
        if cents < 0:
            spent[cat_id] += cents
    out = []
    budgets = db.execute(
        select(models.Budget, C).join(C, models.Budget.category_id == C.id).where(models.Budget.user_id == user_id)
    ).all()
    for budget, cat in budgets:
        used = max(0, -int(spent.get(cat.id, 0) or 0))
        limit = budget.monthly_limit_cents
        out.append(
            {
                "id": budget.id,
                "category_id": cat.id,
                "category": cat.name,
                "color": cat.color,
                "limit": limit,
                "spent": used,
                "percent": round(used * 100 / limit, 1) if limit else 0,
            }
        )
    return sorted(out, key=lambda r: -r["percent"])
