"""Cross-service provider-event dispatch envelope."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Audience an assistant runtime must present when calling back into Orchestra to
# fetch a provider-event's decrypted context. It scopes the ownership-checked
# read path and is distinct from the dispatch-envelope ``audience`` above, which
# names the short-lived credential rail for delivering the dispatch itself.
EVENT_CONTEXT_AUDIENCE = "orchestra:event-context"

# Short-lived internal credential audiences for delivering one dispatch operation
# to the live Unity or offline Communication execution rail.
UNITY_DISPATCH_AUDIENCE = "unity:provider-event-dispatch"
COMMUNICATION_DISPATCH_AUDIENCE = "communication:provider-event-dispatch"


class ProviderEventDispatchRequest(BaseModel):
    """Internal dispatch authorization passed to Communication or Unity."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"] = "1"
    operation_id: str
    run_id: int
    run_key: str
    assistant_id: str
    task_id: int
    binding_id: str
    receipt_id: str
    accepted_revision: str
    source_type: Literal["provider_event"] = "provider_event"
    dispatch_mode: Literal["live", "offline"]
    event_context_ref: str
    issued_at: datetime
    audience: str = Field(
        description="Short-lived internal credential audience for the target rail.",
    )
