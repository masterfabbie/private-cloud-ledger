"""Turns a parsed CSV plus a column mapping into transactions."""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.services import cards
from app.services import csv_parser as cp
from app.services.defaults import LEGACY_CATEGORY_KEYS
from app.services.rules import first_match, get_or_create_tags, load_rules

OPTION_DEFAULTS = {"date_order": "auto", "decimal_style": "auto", "invert_sign": False}


@dataclass
class Row:
    line: int
    booking_date: date
    amount_cents: int
    description: str
    payer: str = ""
    iban: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    imported: int = 0
    duplicates: int = 0
    failed: int = 0
    failed_rows: list[dict] = field(default_factory=list)
    batch_id: int | None = None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def base_key(booking_date: date, amount_cents: int, description: str, payer: str) -> str:
    return f"{booking_date.isoformat()}|{amount_cents}|{_norm(description)}|{_norm(payer)}"


def dedup_hash(key: str, occurrence: int) -> str:
    return hashlib.sha256(f"{key}|{occurrence}".encode()).hexdigest()


def normalize_rows(parsed: cp.ParsedCSV, mapping: dict) -> tuple[list[Row], list[dict]]:
    """Apply the column mapping. Returns (rows, failures) where failures carry file line numbers."""
    opts = {**OPTION_DEFAULTS, **{k: v for k, v in mapping.items() if k in OPTION_DEFAULTS}}
    idx = {h: i for i, h in enumerate(parsed.headers)}

    def col(name: str) -> int | None:
        header = mapping.get(name)
        return idx.get(header) if header else None

    c = {f: col(f) for f in cp.MAPPING_FIELDS}
    if c["date"] is None:
        raise cp.ParseError("Please map the date column")
    if c["amount"] is None and c["debit"] is None and c["credit"] is None:
        raise cp.ParseError("Please map the amount column (or debit/credit columns)")

    rows: list[Row] = []
    failures: list[dict] = []
    for n, raw in enumerate(parsed.rows):
        line = parsed.header_line + n + 1
        get = lambda f: raw[c[f]] if c[f] is not None else ""  # noqa: E731
        try:
            booking_date = cp.parse_date(get("date"), opts["date_order"])
            if c["amount"] is not None:
                cents = cp.parse_amount(get("amount"), opts["decimal_style"])
            else:
                debit = get("debit")
                credit = get("credit")
                if debit.strip():
                    cents = -abs(cp.parse_amount(debit, opts["decimal_style"]))
                elif credit.strip():
                    cents = abs(cp.parse_amount(credit, opts["decimal_style"]))
                else:
                    raise cp.ParseError("Missing amount")
            if c["type"] is not None:
                kind = cp.type_from_value(get("type"))  # unrecognised values keep the amount's sign
                if kind:
                    cents = abs(cents) if kind == "income" else -abs(cents)
            if opts["invert_sign"]:
                cents = -cents
            description = get("description") or get("payer") or "Imported transaction"
            rows.append(
                Row(
                    line=line,
                    booking_date=booking_date,
                    amount_cents=cents,
                    description=description,
                    payer=get("payer"),
                    iban=get("iban").replace(" ", "").upper()[:34],
                    category=get("category"),
                    tags=cp.parse_tags(get("tags")),
                )
            )
        except cp.ParseError as exc:
            failures.append({"line": line, "error": str(exc)})
    return rows, failures


def _category_lookup(db: Session, user_id: int) -> dict[str, models.Category]:
    cats = db.scalars(select(models.Category).where(models.Category.user_id == user_id))
    return {c.name.lower(): c for c in cats}


def resolve_category(db: Session, user_id: int, lookup: dict[str, models.Category], name: str) -> models.Category:
    key = name.strip().lower()
    key = LEGACY_CATEGORY_KEYS.get(key, name.strip()).lower()
    cat = lookup.get(key)
    if cat is None:
        cat = models.Category(user_id=user_id, name=name.strip()[:100], kind="expense")
        db.add(cat)
        db.flush()
        lookup[key] = cat
    return cat


def categorize(
    db: Session,
    user_id: int,
    lookup: dict[str, models.Category],
    rules: list[models.Rule],
    own_ibans: set[str],
    row: Row,
) -> tuple[int | None, list[str]]:
    """Returns (category_id, extra_tags). Priority: CSV column, own-account transfer, rules, Other."""
    if row.category.strip():
        return resolve_category(db, user_id, lookup, row.category).id, []
    if row.iban and row.iban in own_ibans and "transfer" in lookup:
        return lookup["transfer"].id, []
    rule = first_match(rules, row.description, row.payer, row.iban, row.amount_cents)
    if rule:
        return rule.category_id, list(rule.add_tags or [])
    other = lookup.get("other")
    return (other.id if other else None), []


def import_rows(
    db: Session, user: models.User, account: models.Account, rows: list[Row], failures: list[dict], filename: str
) -> ImportResult:
    result = ImportResult(failed=len(failures), failed_rows=failures)
    batch = models.ImportBatch(user_id=user.id, account_id=account.id, filename=filename[:255])
    db.add(batch)
    db.flush()

    existing = set(
        db.scalars(select(models.Transaction.dedup_hash).where(models.Transaction.account_id == account.id))
    )
    lookup = _category_lookup(db, user.id)
    rules = load_rules(db, user.id)
    own_ibans = {
        a.iban.replace(" ", "").upper()
        for a in db.scalars(select(models.Account).where(models.Account.user_id == user.id))
        if a.iban and a.id != account.id
    }

    seen: Counter[str] = Counter()
    for row in rows:
        key = base_key(row.booking_date, row.amount_cents, row.description, row.payer)
        h = dedup_hash(key, seen[key])
        seen[key] += 1
        if h in existing:
            result.duplicates += 1
            continue
        existing.add(h)
        category_id, rule_tags = categorize(db, user.id, lookup, rules, own_ibans, row)
        tx = models.Transaction(
            user_id=user.id,
            account_id=account.id,
            booking_date=row.booking_date,
            amount_cents=row.amount_cents,
            description=row.description[:2000],
            payer=row.payer[:255],
            counterparty_iban=row.iban,
            category_id=category_id,
            import_batch_id=batch.id,
            dedup_hash=h,
        )
        tx.tags = get_or_create_tags(db, user.id, row.tags + rule_tags)
        db.add(tx)
        result.imported += 1

    batch.imported, batch.duplicates, batch.failed = result.imported, result.duplicates, result.failed
    if result.imported == 0:
        db.delete(batch)
    else:
        result.batch_id = batch.id
    db.commit()
    if result.batch_id:
        cards.mark_settlements(db, user.id, batch_id=result.batch_id)
    return result


def manual_dedup_hash(db: Session, account_id: int, booking_date: date, amount_cents: int, description: str, payer: str) -> str | None:
    """Hash for a manually entered transaction, or None if an identical one already exists."""
    key = base_key(booking_date, amount_cents, description, payer)
    h = dedup_hash(key, 0)
    exists = db.scalar(
        select(func.count()).where(models.Transaction.account_id == account_id, models.Transaction.dedup_hash == h)
    )
    return None if exists else h
