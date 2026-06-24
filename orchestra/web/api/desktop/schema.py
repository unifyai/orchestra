from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from orchestra.web.api.utils.safe_text import OptionalSafeLabel, SafeLabel


class DesktopCreate(BaseModel):
    name: SafeLabel = Field(
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
    name: OptionalSafeLabel = Field(
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
    assigned_to_assistant_ids: List[int] = Field(
        default_factory=list,
        description="Agent IDs of every assistant this desktop is linked to",
    )
    sftp_tunnel_id: Optional[str] = Field(
        None,
        description="Relay id of this device's raw-TCP SFTP tunnel (for teardown)",
    )
    created_at: datetime = Field(..., description="When the desktop was registered")
    updated_at: Optional[datetime] = Field(
        None,
        description="When the desktop was last updated",
    )

    class Config:
        orm_mode = True


class DesktopLinkCreate(BaseModel):
    assistant_id: int = Field(
        ...,
        description="Agent ID of the assistant to link this desktop to",
        example=42,
    )
    desktop_id: int = Field(
        ...,
        description="ID of the caller's registered desktop to link",
        example=1,
    )
    filesys_sync: bool = Field(
        False,
        description="Whether to enable filesystem sync for this link",
    )


class DesktopLinkRead(BaseModel):
    assistant_id: int = Field(..., description="Agent ID of the linked assistant")
    desktop_id: int = Field(..., description="ID of the linked desktop")
    owner_user_id: str = Field(..., description="User who owns the linked desktop")
    filesys_sync: bool = Field(..., description="Whether filesystem sync is enabled")

    class Config:
        orm_mode = True


class DesktopPubkeyRead(BaseModel):
    public_key: str = Field(
        ...,
        description="OpenSSH public key to install in the device's authorized_keys",
    )


class SftpTunnelUpdate(BaseModel):
    host: str = Field(..., description="Public SFTP tunnel host (e.g. tunnel.unify.ai)")
    port: int = Field(..., description="Public SFTP tunnel port for this device")


class DesktopSftpTunnelUpdate(BaseModel):
    tunnel_id: str = Field(
        ...,
        description="Relay id of this device's raw-TCP SFTP tunnel",
    )
