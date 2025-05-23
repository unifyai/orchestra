from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, validator


class CreditsResponse(BaseModel):
    """
    Response model for credits models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float


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
