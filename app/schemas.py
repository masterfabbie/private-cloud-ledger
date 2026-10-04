"""API models. Money is always integer cents; amounts are signed (negative = expense)."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import NO_PASSWORD


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- auth / users


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str


class UserOut(ORM):
    id: int
    username: str
    is_admin: bool
    is_active: bool
    created_at: datetime
    has_password: bool = True
    sso: bool = False

    @classmethod
    def of(cls, user) -> "UserOut":
        out = cls.model_validate(user)
        out.has_password = user.password_hash != NO_PASSWORD
        out.sso = user.oidc_sub is not None
        return out


class UserCreate(RegisterIn):
    is_admin: bool = False


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    password: str | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


# ---- accounts


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    bank: str = ""
    iban: str = ""
    currency: str = "EUR"
    opening_balance_cents: int = 0
    opening_date: date | None = None

    @field_validator("iban")
    @classmethod
    def _iban(cls, v: str) -> str:
        return v.replace(" ", "").upper()[:34]


class AccountOut(ORM):
    id: int
    name: str
    bank: str
    iban: str
    currency: str
    opening_balance_cents: int
    opening_date: date | None


# ---- categories / tags


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field("#667eea", pattern=r"^#[0-9A-Fa-f]{6}$")
    kind: str = Field("expense", pattern="^(expense|income|transfer)$")


class CategoryOut(ORM):
    id: int
    name: str
    color: str
    kind: str


# ---- transactions


class TransactionIn(BaseModel):
    account_id: int
    booking_date: date
    amount_cents: int
    description: str = Field(min_length=1, max_length=2000)
    payer: str = ""
    counterparty_iban: str = ""
    category_id: int | None = None
    notes: str = ""
    tags: list[str] = []


class TransactionPatch(BaseModel):
    account_id: int | None = None
    booking_date: date | None = None
    amount_cents: int | None = None
    description: str | None = None
    payer: str | None = None
    category_id: int | None = None
    notes: str | None = None
    tags: list[str] | None = None


class TransactionOut(BaseModel):
    id: int
    account_id: int
    account_name: str
    booking_date: date
    amount_cents: int
    type: str
    description: str
    payer: str
    counterparty_iban: str
    category_id: int | None
    category_name: str | None
    category_color: str | None
    notes: str
    tags: list[str]
    import_batch_id: int | None


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    total: int
    page: int
    page_size: int


class BulkCategorize(BaseModel):
    transaction_ids: list[int]
    category_id: int | None


# ---- rules / budgets / recurring


class RuleIn(BaseModel):
    field: str = "payer"
    match: str = "contains"
    pattern: str = Field("", max_length=255)  # may be empty when an amount condition is set
    amount_sign: str = "any"
    amount_min_cents: int | None = Field(None, ge=0)
    amount_max_cents: int | None = Field(None, ge=0)
    category_id: int
    add_tags: list[str] = []
    priority: int = 100


class RulePreviewIn(BaseModel):
    """A rule as edited in the form, evaluated without saving it."""

    rule_id: int | None = None  # the rule being edited, so it is not compared with itself
    field: str = "payer"
    match: str = "contains"
    pattern: str = Field("", max_length=255)
    amount_sign: str = "any"
    amount_min_cents: int | None = Field(None, ge=0)
    amount_max_cents: int | None = Field(None, ge=0)
    category_id: int | None = None
    priority: int = 100


class RuleOut(ORM):
    id: int
    field: str
    match: str
    pattern: str
    amount_sign: str
    amount_min_cents: int | None
    amount_max_cents: int | None
    category_id: int
    add_tags: list[str]
    priority: int


class RuleFromTransaction(BaseModel):
    transaction_id: int
    category_id: int
    field: str = "payer"
    apply_existing: bool = True


class BudgetIn(BaseModel):
    category_id: int
    monthly_limit_cents: int = Field(gt=0)


class RecurringOut(ORM):
    id: int
    display_name: str
    typical_amount_cents: int
    interval_days: int
    occurrences: int
    last_date: date
    next_date: date
    category_id: int | None
    status: str
    monthly_cost_cents: int = 0


class RecurringPatch(BaseModel):
    status: str = Field(pattern="^(detected|confirmed|dismissed)$")


# ---- import


class ImportCommit(BaseModel):
    token: str
    account_id: int
    mapping: dict
    save_profile: bool = True
