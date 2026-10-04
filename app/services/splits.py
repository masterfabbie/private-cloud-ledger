"""Splitting one transaction into parts with their own categories (e.g. an Amazon order)."""

import re

from sqlalchemy.orm import Session

from app import models
from app.services import csv_parser as cp
from app.services.rules import first_match, load_rules


class SplitError(ValueError):
    pass


def set_splits(db: Session, tx: models.Transaction, parts: list[dict]) -> None:
    """Replace the parts of a transaction. An empty list removes the split.

    Every part needs a non-zero amount with the same sign as the transaction,
    and the parts must add up exactly to the transaction amount.
    """
    if not parts:
        tx.splits = []
        return
    if len(parts) < 2:
        raise SplitError("A split needs at least two parts.")
    sign = 1 if tx.amount_cents >= 0 else -1
    for p in parts:
        if p["amount_cents"] == 0 or (p["amount_cents"] > 0) != (sign > 0):
            raise SplitError("Each part needs an amount with the same sign as the transaction.")
    total = sum(p["amount_cents"] for p in parts)
    if total != tx.amount_cents:
        diff = (tx.amount_cents - total) / 100
        raise SplitError(f"The parts must add up to the transaction amount ({diff:+.2f} € left to distribute).")
    tx.splits = [
        models.TransactionSplit(
            user_id=tx.user_id, amount_cents=p["amount_cents"], category_id=p.get("category_id"),
            note=(p.get("note") or "").strip()[:255],
        )
        for p in parts
    ]


# A money amount needs decimals (12,99 / 9.99 / 1.234,50) or a currency sign (12 € / EUR 12),
# so quantities like "x 3" or "1kg" are not mistaken for prices.
_NUM = r"(?:\d{1,3}(?:[.,]\d{3})+|\d+)"
_AMOUNT_RE = re.compile(
    rf"[-+]?(?:{_NUM}[.,]\d{{2}}(?!\d)\s?(?:€|EUR)?|{_NUM}\s?(?:€|EUR)|(?:€|EUR)\s?{_NUM}(?:[.,]\d{{2}})?)",
    re.IGNORECASE,
)


def parse_lines(text: str, tx: models.Transaction, db: Session) -> tuple[list[dict], list[str]]:
    """Turn pasted lines like "12,99 Netflix" or "Amazon Echo; 49.99" into parts.

    The last number on a line is the amount; the rest is the note. The category is
    suggested by the user's rules, matched against the note text.
    """
    rules = load_rules(db, tx.user_id)
    sign = 1 if tx.amount_cents >= 0 else -1
    parts, skipped = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        matches = list(_AMOUNT_RE.finditer(line))
        if not matches:
            skipped.append(line)
            continue
        m = matches[-1]
        try:
            cents = abs(cp.parse_amount(m.group(0)))
        except cp.ParseError:
            skipped.append(line)
            continue
        if cents == 0:
            skipped.append(line)
            continue
        note = (line[: m.start()] + " " + line[m.end():]).strip(" \t;,:-|x×")
        note = re.sub(r"\s+", " ", note)
        amount = sign * cents
        rule = first_match(rules, note, note, "", amount) if note else None
        parts.append({"amount_cents": amount, "note": note[:255], "category_id": rule.category_id if rule else None})
    return parts, skipped
