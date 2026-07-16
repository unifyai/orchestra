"""Authored task trigger contracts mirrored from Unity."""

from __future__ import annotations

from typing import Annotated, Any, List, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, Field


class CommunicationTrigger(BaseModel):
    """Inbound communication event that should start the task."""

    kind: Literal["communication"] = "communication"
    medium: str
    from_contact_ids: Optional[List[int]] = None
    omit_contact_ids: Optional[List[int]] = None
    recurring: bool = False


class ProviderEventTrigger(BaseModel):
    """Third-party provider trigger that should start the task."""

    kind: Literal["provider_event"] = "provider_event"
    state: Literal["draft", "enabled", "paused"] = "draft"
    connection_id: str
    backend_id: str
    canonical_app_slug: str
    provider_trigger_slug: str
    trigger_config: dict[str, Any] = Field(default_factory=dict)


def _coerce_trigger_dict(data: Any) -> Any:
    """Infer communication triggers from legacy rows that omit kind."""

    if isinstance(data, dict) and "medium" in data and "kind" not in data:
        return {**data, "kind": "communication"}
    return data


TaskTrigger = Annotated[
    Union[CommunicationTrigger, ProviderEventTrigger],
    Field(discriminator="kind"),
    BeforeValidator(_coerce_trigger_dict),
]


def parse_task_trigger(
    value: Any,
) -> CommunicationTrigger | ProviderEventTrigger | None:
    """Parse one authored trigger payload into the discriminated union."""

    if value is None:
        return None
    if isinstance(value, (CommunicationTrigger, ProviderEventTrigger)):
        return value
    if isinstance(value, dict):
        coerced = _coerce_trigger_dict(value)
        kind = coerced.get("kind")
        if kind == "provider_event":
            return ProviderEventTrigger.model_validate(coerced)
        return CommunicationTrigger.model_validate(coerced)
    raise TypeError(f"Unsupported trigger payload type: {type(value)!r}")
