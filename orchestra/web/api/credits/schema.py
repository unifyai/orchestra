from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, validator

# ---------------------------------------------------------------------------
# Canonical ledger category sets
# ---------------------------------------------------------------------------
# Spending (debit) categories — used by external callers via the public API.
SpendingCategory = Literal["llm", "hire", "resources", "media"]
SPENDING_CATEGORIES: set[str] = {"llm", "hire", "resources", "media"}

# Credit (inflow) categories — recharges, promos, dispute resolutions.
CreditCategory = Literal["recharge", "promo", "refund", "dispute"]
CREDIT_CATEGORIES: set[str] = {"recharge", "promo", "refund", "dispute"}

# The union of both sets for reference; internal/reconciliation routines may
# use free-form strings outside this set (e.g. "void", "stale_pending_recharge").
PUBLIC_CATEGORIES: set[str] = SPENDING_CATEGORIES | CREDIT_CATEGORIES


class CreditsResponse(BaseModel):
    """
    Response model for credits models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float


class DeductCreditsRequest(BaseModel):
    """
    Request model for deducting credits.

    Attributes:
        amount (float): The amount of credits to deduct (must be positive).
        category: Spending category — one of ``llm``, ``hire``,
            ``resources``, or ``media``.
        assistant_id: Optional assistant that incurred the cost.
        user_id: Optional acting user (for org cost attribution).
        description: Human-readable description of the charge.
        detail: Arbitrary JSON metadata (model name, token counts, etc.).
    """

    amount: float
    category: SpendingCategory = "llm"
    assistant_id: int | None = None
    user_id: str | None = None
    description: str | None = None
    detail: dict | None = None

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return v


class DeductCreditsResponse(BaseModel):
    """
    Response model for deduct credits endpoint.

    Attributes:
        previous_credits (float): Credits before deduction.
        deducted (float): Amount deducted.
        current_credits (float): Credits after deduction.
    """

    previous_credits: float
    deducted: float
    current_credits: float


class RechargeCreateSchema(BaseModel):
    user_id: str
    quantity: int
    amount_usd: Decimal
    type: Literal["auto", "payment"]  # "payment" = prepaid, "auto" = bill later
    transaction_id: str | None = None

    @validator("transaction_id", always=True)
    def validate_transaction_id(cls, v, values):
        """transaction_id is required for prepaid payments."""
        if values.get("type") == "payment" and not v:
            raise ValueError("transaction_id required for prepaid payments")
        return v


# --- Transaction history & spending breakdown ---


class TransactionItem(BaseModel):
    id: int
    at: datetime
    amount: float
    balance_after: Optional[float] = None
    category: str
    assistant_id: Optional[int] = None
    user_id: Optional[str] = None
    organization_id: Optional[int] = None
    description: Optional[str] = None
    detail: Optional[dict] = None


class TransactionHistoryResponse(BaseModel):
    transactions: list[TransactionItem]


class SpendingBreakdownResponse(BaseModel):
    month: str
    total: float
    by_category: dict[str, float]
