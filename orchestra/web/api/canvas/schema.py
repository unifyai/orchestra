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


class CanvasBatchQueryRequest(BaseModel):
    """Request body for POST /admin/canvas/{token}/queries.

    The batch form of the alias contract: a canvas typically declares several
    bindings and the frame wants them all on mount, so one round trip carries
    every alias instead of one request per panel. The same rule applies as the
    single form — aliases and nothing else; each named binding executes exactly
    as it was validated and stored at author time.
    """

    aliases: list[str] = Field(
        ...,
        min_length=1,
        max_length=32,
        description="Names of bindings declared on this canvas.",
    )


class CanvasAliasResult(BaseModel):
    """Outcome for one alias inside a batch query.

    Failures are per-alias rather than per-request: one revoked table or typo'd
    alias must not blank the five panels whose bindings are fine, and the frame
    protocol already reports data errors to each panel individually.
    """

    rows: list[dict] = Field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None


class CanvasBatchQueryResponse(BaseModel):
    """Results for a batch of aliases, keyed by alias."""

    results: dict[str, CanvasAliasResult]


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


# ---------------------------------------------------------------------------
# Write plane
# ---------------------------------------------------------------------------


class CanvasActionDescriptor(BaseModel):
    """What the frame is told about one action.

    Deliberately excludes the dispatch target. The canvas knows an action's name,
    label and input shape; it never learns which function, task or request sits
    behind it, so a compromised canvas cannot redirect one or discover what else
    exists to invoke.
    """

    name: str
    label: str
    icon: Optional[str] = None
    input_schema: Optional[dict] = None
    requires_confirmation: bool = False
    destructive: bool = False


class CanvasActionsResponse(BaseModel):
    actions: list[CanvasActionDescriptor]


class InvokeCanvasActionRequest(BaseModel):
    """Request body for POST /admin/canvas/{token}/action.

    An action name, its arguments, and who is asking. As with the read plane the
    caller cannot name a target: the server resolves the name against this
    canvas's own action rows.
    """

    action_name: str = Field(..., min_length=1, max_length=64)
    args: dict = Field(default_factory=dict)
    # Deliberately no client-supplied run key. The dedup key is derived here,
    # from the token, the action, the arguments and a time window — a caller
    # able to choose it could mint a fresh key per click and turn double-click
    # protection off for exactly the actions it matters on.
    requested_by_user_id: Optional[str] = None


class CanvasInvocationResponse(BaseModel):
    """One invocation, as the caller observes it."""

    invocation_id: int
    action_name: str
    status: str
    result: Optional[dict] = None
    error: Optional[str] = None
    run_key: str
    # True when this request matched an existing run rather than starting one.
    deduplicated: bool = False
