"""Schemas for shared spaces and memberships."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SPACE_DESCRIPTION_MIN_LENGTH = 20
SPACE_DESCRIPTION_MAX_LENGTH = 1000
PLACEHOLDER_DESCRIPTION_PREFIX = "placeholder description for space"


def validate_space_description(value: str) -> str:
    """Validate and normalize text that explains a shared space's purpose."""

    description = value.strip()
    if not (
        SPACE_DESCRIPTION_MIN_LENGTH <= len(description) <= SPACE_DESCRIPTION_MAX_LENGTH
    ):
        raise ValueError(
            "description must be 20-1000 characters after trimming whitespace.",
        )
    if description.lower().startswith(PLACEHOLDER_DESCRIPTION_PREFIX):
        raise ValueError("description must not be placeholder text.")
    if len(set(description)) == 1:
        raise ValueError("description must contain more than one repeated character.")
    if not any(character.isalnum() for character in description):
        raise ValueError("description must include letters or numbers.")
    return description


class SpaceMembershipStatus(str, Enum):
    """Membership creation outcomes returned by the member-add endpoint."""

    active = "active"


class SpaceCreate(BaseModel):
    """Request body for creating a space."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(
        ...,
        min_length=SPACE_DESCRIPTION_MIN_LENGTH,
        max_length=SPACE_DESCRIPTION_MAX_LENGTH,
    )
    organization_id: Optional[int] = None

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return validate_space_description(value)


class SpaceUpdate(BaseModel):
    """Request body for updating mutable space fields."""

    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(
        None,
        min_length=SPACE_DESCRIPTION_MIN_LENGTH,
        max_length=SPACE_DESCRIPTION_MAX_LENGTH,
    )

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return validate_space_description(value)


class SpaceRead(BaseModel):
    """Full space representation returned by lifecycle endpoints."""

    space_id: int
    name: str
    description: str
    organization_id: Optional[int] = None
    owner_user_id: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SpaceSummary(BaseModel):
    """Compact space representation for assistant membership lists."""

    space_id: int
    name: str
    description: str
    organization_id: Optional[int] = None
    status: str

    model_config = {"from_attributes": True}


class SpaceMemberCreate(BaseModel):
    """Request body for adding a member target to a space."""

    assistant_id: Optional[int] = None
    member_user_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_target_shape(self):
        has_assistant_id = self.assistant_id is not None
        has_member_user_id = bool(self.member_user_id)
        if has_assistant_id == has_member_user_id:
            raise ValueError(
                "Provide exactly one of assistant_id or member_user_id.",
            )
        return self


class SpaceMember(BaseModel):
    """Live assistant membership in a space."""

    assistant_id: int
    space_id: int
    user_id: str
    organization_id: Optional[int] = None
    added_by: str
    created_at: datetime


class SpaceMembershipResponse(BaseModel):
    """Result of adding an assistant to a space."""

    membership_status: SpaceMembershipStatus
    assistant_id: int
    space_id: int
