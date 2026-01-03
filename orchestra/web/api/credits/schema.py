from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, field_validator, validator


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
    """

    amount: float

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
