from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AssistantCreate(BaseModel):
    """
    Schema for creating a new assistant.
    """

    first_name: str = Field(
        ...,
        description="First name of the assistant",
        example="Ada",
    )
    surname: str = Field(
        ...,
        description="Surname of the assistant",
        example="Lovelace",
    )
    age: int = Field(..., description="Age of the assistant", example=28)
    weekly_limit: float = Field(
        ...,
        description="Weekly time limit for the assistant in hours",
        example=15.75,
    )
    max_parallel: int = Field(
        ...,
        description="Maximum number of parallel tasks the assistant can handle",
        example=2,
    )
    region: str = Field(
        ...,
        description="Geographic region of the assistant",
        example="North America",
    )
    profile_photo: str = Field(
        ...,
        description="URL to the assistant's profile photo",
        example="https://example.com/photos/ada.jpg",
    )
    about: str = Field(
        ...,
        description="Brief description about the assistant",
        example="Mathematician and writer known for work on Analytical Engine",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "region": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "about": "Mathematician and writer known for work on Analytical Engine",
            },
        }


class AssistantRead(AssistantCreate):
    """
    Schema for reading assistant data, extends AssistantCreate with additional fields.
    """

    agent_id: str = Field(
        ...,
        description="Unique identifier for the assistant",
        example="12345",
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when the assistant was created",
        example="2025-04-25T10:30:00Z",
    )
    updated_at: Optional[datetime] = Field(
        None,
        description="Timestamp when the assistant was last updated",
        example="2025-04-26T14:15:00Z",
    )
    phone: Optional[str] = Field(
        None,
        description="Contact phone number for the assistant",
        example="+1-555-123-4567",
    )
    email: Optional[str] = Field(
        None,
        description="Email address for the assistant",
        example="ada.lovelace@example.com",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
                "region": "North America",
                "profile_photo": "https://example.com/photos/ada.jpg",
                "about": "Mathematician and writer known for work on Analytical Engine",
                "phone": "+1-555-123-4567",
                "email": "ada.lovelace@example.com",
                "agent_id": "12345",
                "created_at": "2025-04-25T10:30:00Z",
                "updated_at": "2025-04-26T14:15:00Z",
            },
        }


class AssistantUpdate(BaseModel):
    """
    Schema for updating an existing assistant.
    Only includes fields that can be updated.
    """

    weekly_limit: Optional[float] = Field(
        None,
        description="Weekly time limit for the assistant in hours",
        example=20.5,
    )
    max_parallel: Optional[int] = Field(
        None,
        description="Maximum number of parallel tasks the assistant can handle",
        example=3,
    )
    about: Optional[str] = Field(
        None,
        description="Brief description about the assistant",
        example="Award-winning mathematician specializing in algorithm development",
    )
    phone: Optional[str] = Field(
        None,
        description="Contact phone number for the assistant",
        example="+1-555-987-6543",
    )
    email: Optional[str] = Field(
        None,
        description="Email address for the assistant",
        example="ada.lovelace@newdomain.com",
    )

    class Config:
        orm_mode = True
        schema_extra = {
            "example": {
                "weekly_limit": 20.5,
                "max_parallel": 3,
                "about": "Award-winning mathematician specializing in algorithm development",
                "phone": "+1-555-987-6543",
                "email": "ada.lovelace@newdomain.com",
            },
        }
