from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    LargeBinary,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


NO_PASSWORD = "!"  # password_hash of users who only log in via SSO


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    oidc_sub: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256 of the cookie token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship()


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    bank: Mapped[str] = mapped_column(String(100), default="")
    iban: Mapped[str] = mapped_column(String(34), default="")
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    opening_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    opening_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="checking", server_default="checking")  # checking | savings | credit_card | cash
    # Credit cards: text that identifies the card settlement on the paying (checking) account,
    # e.g. "KREDITKARTENABRECHNUNG". Matching transactions are treated as transfers.
    settlement_pattern: Mapped[str] = mapped_column(String(100), default="", server_default="")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    color: Mapped[str] = mapped_column(String(9), default="#667eea")
    kind: Mapped[str] = mapped_column(String(10), default="expense")  # expense | income | transfer


transaction_tags = Table(
    "transaction_tags",
    Base.metadata,
    Column("transaction_id", ForeignKey("transactions.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(50))


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255))
    imported: Mapped[int] = mapped_column(Integer, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("account_id", "dedup_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    booking_date: Mapped[date] = mapped_column(Date, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)  # negative = expense
    description: Mapped[str] = mapped_column(Text, default="")
    payer: Mapped[str] = mapped_column(String(255), default="")
    counterparty_iban: Mapped[str] = mapped_column(String(34), default="")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL"), nullable=True
    )
    dedup_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    category: Mapped[Category | None] = relationship(lazy="joined")
    account: Mapped[Account] = relationship(lazy="joined")
    tags: Mapped[list[Tag]] = relationship(secondary=transaction_tags, lazy="selectin")
    splits: Mapped[list["TransactionSplit"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan", lazy="selectin", order_by="TransactionSplit.id"
    )


class TransactionSplit(Base):
    """Part of a transaction with its own category, e.g. one item of an Amazon order.
    The parts of a transaction always add up to its amount; statistics use the parts."""

    __tablename__ = "transaction_splits"

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    note: Mapped[str] = mapped_column(String(255), default="")

    transaction: Mapped[Transaction] = relationship(back_populates="splits")
    category: Mapped[Category | None] = relationship(lazy="joined")


class ImportProfile(Base):
    __tablename__ = "import_profiles"
    __table_args__ = (UniqueConstraint("account_id", "header_signature"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    header_signature: Mapped[str] = mapped_column(String(64))
    mapping: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(20), default="payer")  # description | payer | iban
    match: Mapped[str] = mapped_column(String(10), default="contains")  # contains | equals | regex
    pattern: Mapped[str] = mapped_column(String(255))
    amount_sign: Mapped[str] = mapped_column(String(10), default="any")  # any | expense | income
    # Optional bounds on the absolute amount (inclusive); equal bounds mean "exactly".
    amount_min_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_max_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    add_tags: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("user_id", "category_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    monthly_limit_cents: Mapped[int] = mapped_column(Integer)


class RecurringSeries(Base):
    __tablename__ = "recurring_series"
    __table_args__ = (UniqueConstraint("user_id", "payer_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    payer_key: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    typical_amount_cents: Mapped[int] = mapped_column(Integer)
    interval_days: Mapped[int] = mapped_column(Integer)
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    last_date: Mapped[date] = mapped_column(Date)
    next_date: Mapped[date] = mapped_column(Date)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(10), default="detected")  # detected | confirmed | dismissed


class BankConnection(Base):
    """Online-banking access via FinTS (HBCI PIN/TAN) for one bank login."""

    __tablename__ = "bank_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    blz: Mapped[str] = mapped_column(String(8))
    server_url: Mapped[str] = mapped_column(String(255))
    login_name: Mapped[str] = mapped_column(String(100))
    customer_id: Mapped[str] = mapped_column(String(100), default="")
    pin_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet token, only if the user opted in
    tan_mechanism: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tan_medium: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_state: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)  # python-fints deconstruct() blob
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="new")  # new | ok | error | tan_required | pin_error
    last_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    links: Mapped[list["BankAccountLink"]] = relationship(
        back_populates="connection", cascade="all, delete-orphan", order_by="BankAccountLink.id"
    )


class BankAccountLink(Base):
    """One account at the bank, optionally linked to a Proud Ledger account."""

    __tablename__ = "bank_account_links"
    __table_args__ = (UniqueConstraint("connection_id", "iban", "account_number", "subaccount"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("bank_connections.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    iban: Mapped[str] = mapped_column(String(34), default="")
    bic: Mapped[str] = mapped_column(String(11), default="")
    account_number: Mapped[str] = mapped_column(String(30), default="")
    subaccount: Mapped[str] = mapped_column(String(30), default="")
    blz: Mapped[str] = mapped_column(String(8), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    sync_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    synced_until: Mapped[date | None] = mapped_column(Date, nullable=True)

    connection: Mapped[BankConnection] = relationship(back_populates="links")
