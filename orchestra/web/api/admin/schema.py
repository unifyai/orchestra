import datetime
from typing import Optional

from pydantic import BaseModel


class RechargeModelRequest(BaseModel):
    """
    Request model for creating a new recharge.

    Provide exactly one of ``user_id`` or ``organization_id`` to identify the
    billing account to credit.

    Attributes:
        user_id: User ID (for personal billing accounts).
        organization_id: Organization ID (for org billing accounts).
        quantity: The number of credits to add.
        type: Recharge type ("payment", "auto", "promo").
        transaction_id: Stripe transaction id (required for "payment" type).
        target_month: Target month for invoice grouping (format: "YYYY-MM").
                      Defaults to current month if not specified.
    """

    user_id: Optional[str] = None
    organization_id: Optional[int] = None
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
        billing_account_id (Optional[int]): The billing account ID.
    """

    model_config = {"from_attributes": True}

    id: str
    billing_account_id: Optional[int] = None


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
        billing_account_id (int): The billing account ID.
        at (datetime): The time of the recharge.
        quantity (float): The quantity of the recharge.
        type (str): The type of the recharge.
    """

    model_config = {"from_attributes": True}

    id: int
    at: datetime.datetime
    billing_account_id: int
    quantity: float
    type: str


class CreditCardFingerprintModelResponse(BaseModel):
    model_config = {"from_attributes": True}

    billing_account_id: int
    fingerprint: str


# Organization list schemas for admin endpoint
class OrganizationListItem(BaseModel):
    """Response model for a single organization in the list."""

    id: int
    name: str
    owner_id: str
    created_at: Optional[datetime.datetime]
    member_count: int


class OrganizationListResponse(BaseModel):
    """Response model for listing all organizations."""

    organizations: list[OrganizationListItem]
    limit: int
    offset: int
