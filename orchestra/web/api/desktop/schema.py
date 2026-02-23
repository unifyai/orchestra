from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DesktopCreate(BaseModel):
    name: str = Field(
        ...,
        description="Human-readable label for the desktop",
        example="Julia's MacBook Pro",
    )
    url: str = Field(
        ...,
        description="Public URL from the tunnel service",
        example="https://abc123.tunnel.unify.ai",
    )
    os: Literal["ubuntu", "windows", "macos"] = Field(
        ...,
        description="Operating system of the desktop",
        example="macos",
    )


class DesktopUpdate(BaseModel):
    name: Optional[str] = Field(
        None,
        description="Human-readable label for the desktop",
        example="Julia's MacBook Pro",
    )
    url: Optional[str] = Field(
        None,
        description="Public URL from the tunnel service",
        example="https://abc123.tunnel.unify.ai",
    )
    os: Optional[Literal["ubuntu", "windows", "macos"]] = Field(
        None,
        description="Operating system of the desktop",
        example="macos",
    )


class DesktopRead(BaseModel):
    id: int = Field(..., description="Desktop ID")
    user_id: str = Field(..., description="Owner user ID")
    name: str = Field(..., description="Human-readable label")
    url: str = Field(..., description="Public tunnel URL")
    os: str = Field(..., description="Operating system")
    assigned_to_assistant_id: Optional[int] = Field(
        None,
        description="Agent ID of the assistant this desktop is assigned to, or null",
    )
    created_at: datetime = Field(..., description="When the desktop was registered")
    updated_at: Optional[datetime] = Field(
        None,
        description="When the desktop was last updated",
    )

    class Config:
        orm_mode = True
