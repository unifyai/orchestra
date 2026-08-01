from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from orchestra.services.task_machine_state_service import TASK_MACHINE_PROJECT_NAME


class TaskExecutionLookupRequest(BaseModel):
    """Lookup one projected open task execution row by assistant/task id."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(description="Assistant identifier that owns the task.")
    task_id: int = Field(description="Logical task identifier.")
    destination: Optional[str] = Field(
        default=None,
        description="Team destination for the task definition, if any.",
    )


class TaskExecutionLookupResponse(BaseModel):
    """Internal open-execution lookup response."""

    execution: Optional[Dict[str, Any]] = Field(
        default=None,
        description="The current open execution row payload, or null when missing.",
    )


class TaskExecutionReprojectRequest(BaseModel):
    """Reproject one task row into its current machine execution state."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(description="Assistant identifier that owns the task.")
    task_id: int = Field(description="Logical task identifier to reproject.")


class TaskExecutionReprojectResponse(BaseModel):
    """Result of reprojecting one task's open execution state."""

    upserted: int = Field(description="Number of execution rows upserted.")
    deleted: int = Field(description="Number of execution rows deleted.")
    execution: Optional[Dict[str, Any]] = Field(
        default=None,
        description="The execution payload after reprojection, or null when unarmed.",
    )


class TaskExecutionCreateOrAdoptRequest(BaseModel):
    """Create a task run by run_key if absent, or adopt the existing row."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    run_key: str = Field(description="Idempotency key for this execution attempt.")
    assistant_id: str = Field(description="Assistant identifier that owns the run.")
    task_id: int = Field(description="Logical task identifier.")
    source_task_log_id: Optional[int] = Field(
        default=None,
        description="Owning Unity/Tasks row for the execution instance.",
    )
    wake: str = Field(
        description="Why the run exists: scheduled, triggered, explicit, provider_event, etc.",
    )
    delivery: Literal["live", "offline"] = Field(
        description="Which delivery lane owns the run.",
    )
    revision: Optional[str] = Field(
        default=None,
        description="Execution revision adopted when the run was created.",
    )
    destination: Optional[str] = Field(
        default=None,
        description=(
            "Owning surface for a team-scoped run (e.g. `team-11`). Part of "
            "`run_key`, and the dispatcher resolves the current execution by it, "
            "so a run created without it can never be found and never fires."
        ),
    )
    scheduled_for: Optional[datetime] = Field(
        default=None,
        description=(
            "Canonical due time for this occurrence. Stays un-jittered so it can "
            "key run_key and anchor the next slot."
        ),
    )
    dispatch_offset_seconds: Optional[float] = Field(
        default=None,
        description="Seconds to add to scheduled_for when dispatching (jitter).",
    )
    entrypoint: Optional[int] = Field(
        default=None,
        description=(
            "Symbolic function id the definition binds this occurrence to. "
            "Dispatch reads it from the row; a runtime-projected occurrence "
            "stored without it dispatches as agentic and a symbolic task then "
            "refuses the run as an entrypoint mismatch."
        ),
    )
    source_medium: Optional[str] = Field(
        default=None,
        description="Inbound medium that triggered the run when applicable.",
    )
    source_ref: Optional[str] = Field(
        default=None,
        description="Stable reference for the triggering communication or wake.",
    )
    source_contact_id: Optional[str] = Field(
        default=None,
        description="Contact identifier associated with the triggering inbound.",
    )
    source_contact_display_name: Optional[str] = Field(
        default=None,
        description="Human-readable contact name associated with the triggering inbound.",
    )
    task_name: Optional[str] = Field(
        default=None,
        description="Human-readable task title mirrored into the run row.",
    )
    started_at: Optional[datetime] = Field(
        default=None,
        description="Optional explicit run start timestamp.",
    )
    state: str = Field(
        default="scheduled",
        description="Initial machine state for the run lifecycle.",
    )
    result_summary: Optional[str] = Field(
        default=None,
        description="Optional hidden run summary.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Optional hidden error payload.",
    )


class TaskExecutionUpdateRequest(BaseModel):
    """Apply a partial update to an existing task run row."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(
        description="Assistant identifier that owns the run being updated.",
    )
    run_key: str = Field(description="Idempotency key for the run to update.")
    source_task_log_id: Optional[int] = Field(
        default=None,
        description="Physical Tasks-row log id; resolves team-task runs to "
        "their team surface (mirrors create-or-adopt resolution).",
    )
    updates: Dict[str, Any] = Field(
        description="Partial field updates to merge into the run row payload.",
    )


class TaskSourceReleaseRequest(BaseModel):
    """Terminalize executions left running after their offline worker vanished."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(
        description="Assistant identifier used to resolve the Assistants project.",
    )
    source_task_log_id: int = Field(
        description="Physical Tasks-row log id whose running executions to release.",
    )
    info: Optional[str] = Field(
        default=None,
        description="Optional diagnostic note stored on each released execution.",
    )
    run_key: Optional[str] = Field(
        default=None,
        description=(
            "Release only this run. Omit to release every running execution "
            "under the definition, which also terminalizes any sibling "
            "occurrence that has already started."
        ),
    )


class TaskSourceReleaseResponse(BaseModel):
    """Outcome of one stuck-execution release attempt.

    Transitions are audited on the execution rows themselves, which is where
    run state lives. There is no definition status to record a before/after
    for: this operation cannot reach a definition.
    """

    updated: bool = Field(
        description="True when at least one running execution was terminalized.",
    )
    source_task_log_id: int = Field(
        description="Physical Tasks-row log id whose executions were targeted.",
    )
    released_run_keys: list[str] = Field(
        default_factory=list,
        description="run_key of every execution moved to failed.",
    )
    reprojected: bool = Field(
        default=False,
        description=(
            "True when the definition regained an open occurrence. Recurrence is "
            "computed at dispatch, so a worker that died before projecting its "
            "successor leaves the series with nothing to fire; releasing the run "
            "re-projects the head so a crash costs one occurrence, not the series."
        ),
    )
    reason: str = Field(
        description="Why rows were or were not updated (released/no_running_executions).",
    )


class TaskExecutionLatestRequest(BaseModel):
    """Lookup the latest run row for one assistant/task pair."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(description="Assistant identifier that owns the run.")
    task_id: int = Field(description="Logical task identifier.")
    source_task_log_id: Optional[int] = Field(
        default=None,
        description="Optional Tasks row log id used to resolve assistant-scoped contexts.",
    )


class TaskExecutionLatestResponse(BaseModel):
    """Latest task run lookup response."""

    run: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Most recently updated run row payload, or null when absent.",
    )


class TaskExecutionGetRequest(BaseModel):
    """Lookup one task run row by its idempotency key."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(description="Assistant identifier that owns the run.")
    run_key: str = Field(description="Idempotency key for the run to fetch.")
    source_task_log_id: Optional[int] = Field(
        default=None,
        description="Optional Tasks row log id used to resolve assistant-scoped contexts.",
    )


class TaskExecutionGetResponse(BaseModel):
    """Task run lookup response keyed by run_key."""

    run: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Run row payload when present, or null when absent.",
    )


class TaskExecutionMutationResponse(BaseModel):
    """Serialized task run payload returned by internal mutation endpoints."""

    run: Dict[str, Any] = Field(description="Materialized run row payload.")
    created: Optional[bool] = Field(
        default=None,
        description="Whether the mutation created a new run row.",
    )


class TaskOutboundOperationCreateOrAdoptRequest(BaseModel):
    """Create an outbound operation by operation_key if absent, or adopt it."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    operation_key: str = Field(
        description="Idempotency key for this outbound communication attempt.",
    )
    assistant_id: str = Field(
        description="Assistant identifier that owns the outbound attempt.",
    )
    task_run_key: str = Field(description="Owning task run key for the operation.")
    task_id: Optional[int] = Field(
        default=None,
        description="Logical task identifier when the outbound attempt came from a task.",
    )
    source_task_log_id: Optional[int] = Field(
        default=None,
        description="Owning Unity/Tasks row for the outbound attempt.",
    )
    operation_index: int = Field(
        description="Stable ordinal for this outbound attempt within the task run.",
    )
    method_name: str = Field(
        description="Comms primitive method used for the outbound attempt.",
    )
    medium: str = Field(description="Communication medium used for the attempt.")
    target_kind: str = Field(
        description="Target category such as contact, discord_channel, or email.",
    )
    contact_id: Optional[int] = Field(
        default=None,
        description="Resolved contact identifier when the target is contact-anchored.",
    )
    target_metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Serialized destination details for the outbound attempt.",
    )
    status: str = Field(
        default="pending",
        description="Initial ledger state for the outbound attempt.",
    )
    provider_message_id: Optional[str] = Field(
        default=None,
        description="Provider-specific delivery identifier when available.",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="Optional explicit creation timestamp.",
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Optional explicit update timestamp.",
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Optional explicit completion timestamp.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Optional hidden error payload for failed attempts.",
    )


class TaskOutboundOperationUpdateRequest(BaseModel):
    """Apply a partial update to an existing outbound operation row."""

    project_name: str = Field(
        default=TASK_MACHINE_PROJECT_NAME,
        description="Project that owns the internal task machine contexts.",
    )
    assistant_id: str = Field(
        description="Assistant identifier that owns the outbound operation.",
    )
    operation_key: str = Field(
        description="Idempotency key for the outbound operation to update.",
    )
    source_task_log_id: Optional[int] = Field(
        default=None,
        description="Physical Tasks-row log id; resolves team-task operations "
        "to their team surface (mirrors create-or-adopt resolution).",
    )
    updates: Dict[str, Any] = Field(
        description="Partial field updates to merge into the outbound operation row.",
    )


class TaskOutboundOperationMutationResponse(BaseModel):
    """Serialized outbound operation payload returned by internal endpoints."""

    operation: Dict[str, Any] = Field(
        description="Materialized outbound operation row payload.",
    )
    created: Optional[bool] = Field(
        default=None,
        description="Whether the mutation created a new outbound operation row.",
    )


class ProviderEventContextRequest(BaseModel):
    """Ownership-scoped fetch of one provider-event's decrypted context.

    The assistant runtime presents the run/receipt identifiers it received in
    the dispatch envelope; the handler fails closed unless the caller owns the
    assistant, the run resolves to the referenced task, and the receipt still
    exposes the requested event-context reference.
    """

    model_config = ConfigDict(extra="forbid")

    assistant_id: str = Field(description="Assistant identifier that owns the run.")
    task_id: int = Field(description="Logical task identifier for the run.")
    run_id: int = Field(description="Stable run identifier (the run's log_event id).")
    receipt_id: str = Field(description="Durable provider-event receipt identifier.")
    event_context_ref: str = Field(
        description="Opaque reference to the receipt's committed event context.",
    )
    audience: str = Field(
        description="Credential audience; must be the event-context read audience.",
    )
    issued_at: datetime = Field(
        description="UTC instant when the runtime issued this context fetch.",
    )


class ProviderEventContextResponse(BaseModel):
    """Decrypted provider-event context bundle for one accepted run."""

    receipt_id: str = Field(description="Durable provider-event receipt identifier.")
    run_id: int = Field(description="Run identifier echoed from the request.")
    event_context_ref: str = Field(
        description="Opaque reference to the receipt's committed event context.",
    )
    envelope: Dict[str, Any] = Field(
        description="Stable provider-event envelope recorded on the receipt.",
    )
    curated_projection: Dict[str, Any] = Field(
        description="Curated provider-event projection recorded on the receipt.",
    )
    source_body: Any = Field(
        description="Decrypted source payload, parsed as JSON when possible.",
    )
    expires_at: datetime | None = Field(
        default=None,
        description="UTC instant when this context becomes unreadable.",
    )


class TaskSupervisorSweepResponse(BaseModel):
    """Summary of one supervisor sweep over enabled, armed task definitions."""

    started_at: str = Field(description="UTC instant the sweep began.")
    finished_at: str = Field(description="UTC instant the sweep finished.")
    projects_scanned: int = Field(
        description="Task-machine projects visited (one per owner).",
    )
    surfaces_scanned: int = Field(
        description="Task-surface contexts holding enabled, armed definitions.",
    )
    definitions_scanned: int = Field(
        description="Enabled, armed definitions re-projected this pass.",
    )
    upserted: int = Field(
        description=(
            "Real projection writes. A healthy fleet sweeps to zero; a "
            "non-zero count is the number of dropped batons just healed."
        ),
    )
    deleted: int = Field(
        description="Stale open heads removed for gone or ineligible tasks.",
    )
    errors: List[str] = Field(
        default_factory=list,
        description="Per-surface failures; the sweep continues past them.",
    )
