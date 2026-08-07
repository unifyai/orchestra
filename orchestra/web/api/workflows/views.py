"""Waking an assistant to carry out a recorded workflow install-state change.

Console cannot install a workflow itself: planting content fans out over the
custom-sync engine, which only the assistant has. So Console records the intent
as a ``Workflows/Requests`` row in the assistant's own context and calls this to
say one exists.

The division of labour is deliberate, and differs from the canvas invocation
route next door. There, the viewer is anonymous and must never hold a data key,
so Orchestra writes the row itself. Here the caller is Console, already
authenticated as the owner and already writing that context for every other
manager, so it owns the write and mints the idempotency key. This route only
dispatches — which is why a dispatch failure is reported rather than raised: the
row is the mechanism, the wake is an optimisation, and the assistant's boot
sweep drains the same queue either way.
"""

import logging

from fastapi import APIRouter

from orchestra.web.api.utils.assistant_infra import _post_unity_system_event
from orchestra.web.api.workflows.schema import (
    DispatchWorkflowRequest,
    DispatchWorkflowResponse,
)

logger = logging.getLogger(__name__)

admin_router = APIRouter()


@admin_router.post(
    "/workflows/requests/dispatch",
    response_model=DispatchWorkflowResponse,
)
async def admin_dispatch_workflow_request(
    body: DispatchWorkflowRequest,
) -> DispatchWorkflowResponse:
    """Ask an assistant to drain its recorded workflow requests.

    Never fails the caller on a dispatch problem. The request is already
    durable, so an Adapters outage or a sleeping fleet means the change applies
    on the next wake instead of not at all — and telling Console "this failed"
    would be false.
    """
    try:
        await _post_unity_system_event(
            assistant_id=body.assistant_id,
            event_type="workflow_request",
            message=(f"{body.action} was requested for the '{body.slug}' workflow."),
            extra_event_fields={
                "type": "workflow_request",
                "request_id": body.request_id,
                "slug": body.slug,
                "action": body.action,
                "destination": body.destination,
            },
        )
    except Exception as error:
        logger.warning(
            "Workflow request %s recorded but not dispatched to assistant %s: %s",
            body.request_id,
            body.assistant_id,
            error,
        )
        return DispatchWorkflowResponse(
            request_id=body.request_id,
            dispatched=False,
            detail="Recorded; it will be applied the next time the assistant wakes.",
        )

    return DispatchWorkflowResponse(request_id=body.request_id, dispatched=True)
