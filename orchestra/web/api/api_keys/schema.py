"""API key management schemas."""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ApiKeyResponse(BaseModel):
    """Response model for an API key."""

    id: int
    name: str
    key_prefix: str = Field(
        description="First 8 characters of the key for identification",
    )
    created_at: datetime
    organization_id: Optional[int] = None
    organization_name: Optional[str] = None

    model_config = {"from_attributes": True}


class ApiKeysListResponse(BaseModel):
    """Response model for listing API keys."""

    personal_keys: List[ApiKeyResponse]
    organization_keys: Dict[str, List[ApiKeyResponse]] = Field(
        description="Organization keys grouped by organization name",
    )


class RevokeApiKeyRequest(BaseModel):
    """Request to revoke an API key."""

    key_id: int
