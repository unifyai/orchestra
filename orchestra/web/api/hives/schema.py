"""Pydantic schemas for Hive API endpoints."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class HiveCreate(BaseModel):
    """Request body for creating a Hive."""

    name: str = Field(
        ...,
        description="Display name for the Hive (e.g. 'Midland Heart Patches')",
        max_length=120,
    )
    description: Optional[str] = Field(
        None,
        description="Optional free-text description of this Hive's purpose",
    )


class HiveRead(BaseModel):
    """Full Hive representation returned by all Hive endpoints."""

    model_config = ConfigDict(from_attributes=True)

    hive_id: int = Field(..., description="Primary key")
    organization_id: int = Field(..., description="Owning organization")
    name: str
    description: Optional[str]
    status: str = Field(..., description="'active' or 'deleting'")
    created_at: datetime
    updated_at: datetime


class HiveUpdate(BaseModel):
    """Request body for renaming or re-describing a Hive."""

    name: Optional[str] = Field(None, max_length=120)
    description: Optional[str] = None


class HiveSummary(BaseModel):
    """Compact Hive reference embedded in ``AssistantRead``."""

    model_config = ConfigDict(from_attributes=True)

    hive_id: int
    name: str
