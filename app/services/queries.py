"""Shared transaction filtering used by the list, stats and export endpoints."""

from dataclasses import dataclass
from datetime import date

from fastapi import Query
from sqlalchemy import Select, and_, extract, or_, select

from app import models


@dataclass
class TxFilters:
    account_id: int | None = None
    year: int | None = None
    month: int | None = None  # 1-12; works with or without a year
    date_from: date | None = None
    date_to: date | None = None
    category_ids: list[int] | None = None
    tag: str | None = None
    q: str | None = None
    kind: str | None = None  # income | expense
    min_amount: float | None = None
    max_amount: float | None = None


def tx_filters(
    account_id: int | None = None,
    year: int | None = None,
    month: int | None = Query(None, ge=1, le=12),
    date_from: date | None = None,
    date_to: date | None = None,
    category_id: list[int] | None = Query(None),
    tag: str | None = None,
    q: str | None = None,
    kind: str | None = Query(None, pattern="^(income|expense)$"),
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> TxFilters:
    return TxFilters(account_id, year, month, date_from, date_to, category_id, tag, q, kind, min_amount, max_amount)


def apply_filters(stmt: Select, user_id: int, f: TxFilters, *, skip_categories: bool = False) -> Select:
    T = models.Transaction
    stmt = stmt.where(T.user_id == user_id)
    if f.account_id:
        stmt = stmt.where(T.account_id == f.account_id)
    if f.year:
        stmt = stmt.where(extract("year", T.booking_date) == f.year)
    if f.month:
        stmt = stmt.where(extract("month", T.booking_date) == f.month)
    if f.date_from:
        stmt = stmt.where(T.booking_date >= f.date_from)
    if f.date_to:
        stmt = stmt.where(T.booking_date <= f.date_to)
    if f.category_ids and not skip_categories:
        # Split transactions match through their parts, all others through their own category.
        S = models.TransactionSplit
        ids = [i for i in f.category_ids if i > 0]
        unsplit = ~T.splits.any()
        conds = [and_(unsplit, T.category_id.in_(ids)), T.splits.any(S.category_id.in_(ids))] if ids else []
        if 0 in f.category_ids:  # 0 = uncategorized
            conds += [and_(unsplit, T.category_id.is_(None)), T.splits.any(S.category_id.is_(None))]
        stmt = stmt.where(or_(*conds))
    if f.tag:
        stmt = stmt.where(T.tags.any(models.Tag.name == f.tag.lower()))
    if f.q:
        like = f"%{f.q.strip()}%"
        stmt = stmt.where(or_(T.description.ilike(like), T.payer.ilike(like), T.notes.ilike(like)))
    if f.kind == "income":
        stmt = stmt.where(T.amount_cents > 0)
    elif f.kind == "expense":
        stmt = stmt.where(T.amount_cents < 0)
    if f.min_amount is not None:
        stmt = stmt.where(or_(T.amount_cents >= round(f.min_amount * 100), T.amount_cents <= -round(f.min_amount * 100)))
    if f.max_amount is not None:
        cap = round(f.max_amount * 100)
        stmt = stmt.where(T.amount_cents <= cap, T.amount_cents >= -cap)
    return stmt


def filtered_transactions(user_id: int, f: TxFilters) -> Select:
    T = models.Transaction
    return apply_filters(select(T), user_id, f).order_by(T.booking_date.desc(), T.id.desc())
