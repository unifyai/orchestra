from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from orchestra.provider_triggers.task_trigger import TaskTrigger


class TypedTaskResponse(BaseModel):
    """One authored assistant task row."""

    log_event_id: int
    task_id: int
    task_revision: int
    assistant_id: int
    name: str | None = None
    description: str | None = None
    status: str | None = None
    enabled: bool | None = None
    offline: bool | None = None
    requires_filesystem: bool | None = None
    requires_computer: bool | None = None
    trigger: TaskTrigger | dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    priority: str | None = None
    entrypoint: int | None = None
    provider_event_binding_id: str | None = Field(
        default=None,
        alias="provider_event_binding_id",
    )
    raw: dict[str, Any] = Field(default_factory=dict)


class TypedTaskListResponse(BaseModel):
    tasks: list[TypedTaskResponse]


class TypedTaskCreateRequest(BaseModel):
    name: str
    description: str
    trigger: TaskTrigger | dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    status: str | None = None
    enabled: bool = True
    offline: bool = False
    requires_filesystem: bool = False
    requires_computer: bool = False
    priority: str = "normal"
    entrypoint: int | None = None


class TypedTaskPatchRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    trigger: TaskTrigger | dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    enabled: bool | None = None
    offline: bool | None = None
    requires_filesystem: bool | None = None
    requires_computer: bool | None = None
    priority: str | None = None
    entrypoint: int | None = None
    status: str | None = None


class TaskRevisionConflictResponse(BaseModel):
    code: Literal["task_revision_conflict"] = "task_revision_conflict"
    task_revision: int


class TriggerHealthResponse(BaseModel):
    task_id: int
    task_revision: int
    authored_trigger_state: str | None = None
    task_enabled: bool = True
    runtime_health: Literal[
        "absent",
        "provisioning",
        "healthy",
        "recovering",
        "needs_attention",
        "removing",
    ] = "absent"
    desired_activation_revision: str | None = None
    observed_activation_revision: str | None = None
    acceptance_epoch: int | None = None
    local_acceptance_open: bool = False
    active_generation_id: str | None = None
    coverage_started_at: str | None = None
    coverage_ended_at: str | None = None
    remediation: str | None = None
    event_storage_configured: bool = False


class StagedProviderTrigger(BaseModel):
    backend_id: str
    canonical_app_slug: str
    provider_trigger_slug: str
    provider_version: str | None = None
    display_name: str | None = None
    description: str | None = None
    config_schema: dict[str, Any] = Field(default_factory=dict)


class TriggerCatalogResponse(BaseModel):
    available: bool = True
    unavailable_reason: str | None = None
    triggers: list[StagedProviderTrigger] = Field(default_factory=list)


class RetryTriggerResponse(BaseModel):
    task_id: int
    status: Literal["accepted"] = "accepted"


class TaskTriggerRequest(BaseModel):
    assistant_id: int = Field(
        description="The assistant that owns the task to trigger.",
        examples=[1406],
    )


class TaskTriggerStatus(BaseModel):
    task_id: int = Field(description="The logical task id requested by the caller.")
    assistant_id: int = Field(description="The assistant that owns the triggered task.")
    status: str = Field(
        default="accepted",
        description="Immediate dispatch status for the asynchronous task trigger.",
    )
