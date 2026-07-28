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


class CanvasQueryRequest(BaseModel):
    """Request body for POST /admin/canvas/{token}/query.

    An alias and nothing else. This is the whole point of the endpoint: the
    dashboard tile bridge it replaces accepts ``context`` and ``filter`` from the
    client, so any token holder can query anything in the creator's project. Here
    the server loads the canvas record and executes the binding that was
    validated and stored at author time, so a compromised or prompt-injected
    canvas cannot widen its own reach — the worst it can do is ask for one of its
    own declared aliases.

    There is deliberately no ``limit`` either. Row caps were bounded when the
    binding was authored and travel with it.
    """

    alias: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z_$][A-Za-z0-9_$]*$",
        description="Name of a binding declared on this canvas.",
    )


class CanvasQueryResponse(BaseModel):
    """Rows for one binding alias.

    Always a list, whatever the binding's operation. A reduction yields a scalar
    or a grouped mapping, and normalising here means an authored canvas never has
    to branch on which operation produced its data: a scalar arrives as
    ``[{"value": …}]`` and a mapping as a single row. The same normalisation is
    applied to the author-time dry-run samples, so the preview and the live view
    hand the canvas the same shape.
    """

    alias: str
    rows: list[dict]
    # True when the binding's own row cap was reached, so the canvas can say so
    # rather than quietly presenting a partial set as complete.
    truncated: bool = False


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
