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

    class Config:
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
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

    class Config:
        schema_extra = {
            "example": {
                "first_name": "Ada",
                "surname": "Lovelace",
                "age": 28,
                "weekly_limit": 15.75,
                "max_parallel": 2,
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

    class Config:
        schema_extra = {"example": {"weekly_limit": 20.5, "max_parallel": 3}}
