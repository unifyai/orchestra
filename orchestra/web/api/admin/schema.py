import datetime
from typing import Any, Dict, Optional

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


class FileWriteRequest(BaseModel):
    """Schema for file write/update request.

    Notes:
        - `files` maps relative file paths to base64-encoded contents (bytes).
        - All files, including text, should be provided as base64 to avoid
          accidental text decoding. Clients should base64-encode on write and
          base64-decode on read.
    """

    user_id: str
    project_name: str
    files: dict[str, str]
    staging: bool = False


class FileUploadUrlRequest(BaseModel):
    """Schema for creating a signed resumable upload URL for GCS.

    Attributes:
        user_id: Owner of the project.
        project_name: Project name.
        path: Relative file path within the project (no leading slash).
        content_type: Optional MIME type to set on the object.
        staging: Whether to use the staging bucket.
    """

    user_id: str
    project_name: str
    path: str
    content_type: Optional[str] = None
    staging: bool = False


# Contact schema for admin_list_contacts endpoint
class Contact(BaseModel):
    # the ID of the user who owns this contact log
    user_id: Optional[str]
    first_name: Optional[str]
    surname: Optional[str]
    email_address: Optional[str]
    phone_number: Optional[str]
    whatsapp_number: Optional[str]
    description: Optional[str]
    custom_fields: Dict[str, Any] = {}


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
