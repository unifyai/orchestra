"""Pydantic schemas for canvas token endpoints."""

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

# Kept in step with the CHECK constraints on ``canvas_token``. Literal types give
# the caller a 422 naming the allowed values, and the constraints make the same
# rule true of any row that reaches the table by another path.
Visibility = Literal["private", "team", "public_link"]
Status = Literal["draft", "published", "quarantined"]


class RegisterCanvasTokenRequest(BaseModel):
    """Request body for POST /canvas/tokens."""

    token: str = Field(..., min_length=1, max_length=12)
    context_name: str = Field(..., min_length=1, max_length=500)
    project_name: str = Field(..., min_length=1, max_length=200)
    visibility: Visibility = "private"
    status: Status = "draft"


class UpdateCanvasTokenRequest(BaseModel):
    """Request body for PATCH /canvas/tokens/{token}.

    Only the two operational columns are mutable. Publishing a draft and
    quarantining a live canvas both have to take effect without rewriting the
    canvas or reissuing its URL, which is why they are not part of the authored
    record.
    """

    visibility: Optional[Visibility] = None
    status: Optional[Status] = None

    @model_validator(mode="after")
    def _require_one(self) -> "UpdateCanvasTokenRequest":
        if self.visibility is None and self.status is None:
            raise ValueError("provide at least one of 'visibility' or 'status'")
        return self


class CanvasTokenResponse(BaseModel):
    """Response for canvas token registration and update."""

    token: str
    context_name: str
    visibility: Visibility
    status: Status


class CanvasTokenResolutionResponse(BaseModel):
    """Response for admin token resolution.

    Console resolves a token to the owner's identity, then reads the canvas row
    from the Unify context as that owner. ``visibility`` and ``status`` come back
    on the same call because every read path has to check them before it uses the
    admin key, and a second round trip on that path would be pure latency.
    """

    context_name: str
    user_id: str
    organization_id: Optional[int] = None
    project_id: int
    project_name: str
    visibility: Visibility
    status: Status
