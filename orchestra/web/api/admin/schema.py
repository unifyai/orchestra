import datetime
from typing import Optional

from pydantic import BaseModel


class RechargeModelRequest(BaseModel):
    """
    Request model for creating new recharge model.

    Attributes:
        user_id (str): The id of the user.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
        transaction_id (Optional[str]): The transaction id (required for payment type).
        target_month (Optional[str]): Target month for invoice grouping (format: "2025-06").
                                     Defaults to current month if not specified.
    """

    user_id: str
    quantity: float
    type: str
    transaction_id: Optional[str] = None
    target_month: Optional[str] = None


class RechargeTypeModelRequest(BaseModel):
    """
    Request model for creating new recharge_type model.

    Attributes:
        type (str): The type of the recharge_type.
    """

    type: str


class UsersModelResponse(BaseModel):
    """
    Response model for users models.

    Attributes:
        id (str): The id of the users.
        credits (float): The credits of the users.
    """

    id: str
    credits: float
    stripe_customer_id: Optional[str]
    autorecharge: bool
    autorecharge_threshold: float
    autorecharge_qty: float


class RechargeTypeModelResponse(BaseModel):
    """
    Response model for recharge_type models.

    Attributes:
        type (str): The type of the recharge_type.
    """

    type: str


class RechargeModelResponse(BaseModel):
    """
    Response model for recharge models.

    Attributes:
        id (int): The id of the recharge.
        user_id (str): The id of the user.
        at (datetime): The time of the recharge.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
    """

    id: int
    at: datetime.datetime
    user_id: str
    quantity: float
    type: str


class CreditCardFingerprintModelResponse(BaseModel):
    user_id: str
    fingerprint: str


# Organization list schemas for admin endpoint
class OrganizationListItem(BaseModel):
    """Response model for a single organization in the list."""

    id: int
    name: str
    owner_id: str
    billing_user_id: Optional[str]
    created_at: Optional[datetime.datetime]
    member_count: int


class OrganizationListResponse(BaseModel):
    """Response model for listing all organizations."""

    organizations: list[OrganizationListItem]
    limit: int
    offset: int
