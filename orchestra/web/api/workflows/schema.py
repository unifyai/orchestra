"""Pydantic schemas for the workflow request dispatch endpoint."""

from typing import Literal

from pydantic import BaseModel, Field

# The install-state changes a reading surface may ask for. Kept in step with
# ``unify.workflow_manager.types.request.ACTIONS``; a Literal gives the caller a
# 422 naming the allowed values instead of a wake that settles as failed.
Action = Literal["install", "uninstall", "update", "save_params"]


class DispatchWorkflowRequest(BaseModel):
    """Request body for POST /admin/workflows/requests/dispatch.

    Carries identifiers only. What to do is already on the ``Workflows/Requests``
    row the caller wrote, which is what lets a lost dispatch cost latency rather
    than the change itself.
    """

    assistant_id: int = Field(..., ge=0)
    request_id: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=200)
    action: Action
    destination: str = Field("personal", min_length=1, max_length=200)


class DispatchWorkflowResponse(BaseModel):
    """What the caller learns from asking for a wake.

    ``dispatched`` false is not an error: the row is durable and the assistant's
    boot sweep drains the same queue, so the honest report is "recorded, not yet
    woken" rather than a failure the surface should show as one.
    """

    request_id: str
    dispatched: bool
    detail: str = ""
