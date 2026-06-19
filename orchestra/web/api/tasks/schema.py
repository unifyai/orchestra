from pydantic import BaseModel, Field


class TaskTriggerStatus(BaseModel):
    task_id: int = Field(description="The logical task id requested by the caller.")
    assistant_id: int = Field(description="The assistant that owns the triggered task.")
    status: str = Field(
        default="accepted",
        description="Immediate dispatch status for the asynchronous task trigger.",
    )
